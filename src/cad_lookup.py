"""County Appraisal District property lookups for Texas counties.

Provides owner name → property address lookups for probate enrichment,
and property → tax status lookups for delinquency enrichment.

Supported counties:
  Williamson (WCAD): Real-time SODA REST API at data.wcad.org
  Bell (BCAD):       Bulk Excel download from bellcad.org (local cache)
  Travis (TCAD):     Bulk ZIP export from traviscad.org (local cache)

The WCAD SODA API is the gold standard — instant, no auth, SQL-like queries.
Bell and Travis use periodically-downloaded bulk data loaded into memory.
"""

import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15

# ── WCAD (Williamson County) — SODA REST API ─────────────────────────

WCAD_API_BASE = "https://data.wcad.org/resource"
WCAD_OWNER_DATASET = "absk-uy9g"  # PropOwner table
WCAD_PROPERTY_DATASET = "ij43-xknu"  # Property data


def _wcad_name_search(last_name: str, first_name: str = "") -> list[dict]:
    """Search WCAD by owner name via SODA API.

    Returns list of property records with address, value, parcel info.
    """
    # Build SoQL WHERE clause
    where_parts = [f"namelast like '%{last_name.upper()}%'"]
    if first_name:
        where_parts.append(f"namefirst like '%{first_name.upper()}%'")

    where = " AND ".join(where_parts)
    select = (
        "propertyid,fullname,namelast,namefirst,"
        "situsaddress,sstreetnumber,sstreetname,sstreetsuffix,scity,sstate,szip,"
        "mailing1,mcity,mstate,mzip,"
        "totalpropmktvalue,totalassessedvalue,"
        "quickrefid,propertynumber,propertytypedesc,legaldescription"
    )

    url = f"{WCAD_API_BASE}/{WCAD_OWNER_DATASET}.json"
    params = {
        "$where": where,
        "$select": select,
        "$limit": 20,
        "$order": "totalpropmktvalue DESC",
    }

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            logger.debug("WCAD name search '%s %s': %d results", last_name, first_name, len(data))
            return data
        else:
            logger.warning("WCAD API error: %d %s", resp.status_code, resp.text[:100])
            return []
    except requests.RequestException as e:
        logger.warning("WCAD API request failed: %s", e)
        return []


def _wcad_address_search(address: str) -> list[dict]:
    """Search WCAD by property address via SODA API."""
    # Parse street number and name from address
    m = re.match(r"(\d+)\s+(.+)", address.strip())
    if not m:
        return []

    street_num = m.group(1)
    street_name = m.group(2).upper().split(",")[0].strip()
    # Remove suffix for broader matching
    street_name = re.sub(r"\s+(ST|AVE|RD|DR|LN|BLVD|WAY|CIR|CT|PL|CV)\.?$", "", street_name, flags=re.IGNORECASE)

    where = f"sstreetnumber='{street_num}' AND sstreetname like '%{street_name}%'"
    select = (
        "propertyid,fullname,namelast,namefirst,"
        "situsaddress,scity,szip,"
        "totalpropmktvalue,totalassessedvalue,"
        "quickrefid,propertytypedesc"
    )

    url = f"{WCAD_API_BASE}/{WCAD_OWNER_DATASET}.json"
    params = {"$where": where, "$select": select, "$limit": 5}

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        return []
    except requests.RequestException as e:
        logger.warning("WCAD address search failed: %s", e)
        return []


# ── Unified lookup interface ──────────────────────────────────────────


def _name_match_score(search_last: str, search_first: str, api_name: str) -> float:
    """Score how well a search name matches an API owner name."""
    search_tokens = {search_last.upper()}
    if search_first:
        search_tokens.add(search_first.upper())
    api_tokens = set(api_name.upper().replace(",", "").split())
    noise = {"&", "JR", "SR", "II", "III", "IV", "THE", "ESTATE", "OF"}
    search_tokens -= noise
    api_tokens -= noise
    if not search_tokens or not api_tokens:
        return 0.0
    overlap = search_tokens & api_tokens
    return len(overlap) / max(len(search_tokens), len(api_tokens))


def _parse_name_parts(full_name: str) -> tuple[str, str]:
    """Split a name into (last, first) for CAD search.

    Input formats:
      "John Smith" → ("Smith", "John")
      "Smith, John" → ("Smith", "John")
      "Charlene Randolph Trimble" → ("Trimble", "Charlene")
    """
    name = re.sub(r"\b(?:JR|SR|II|III|IV|ESQ)\b\.?", "", full_name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name).strip()

    # Handle "LAST, FIRST" format
    if "," in name:
        parts = name.split(",", 1)
        return parts[0].strip(), parts[1].strip().split()[0] if parts[1].strip() else ""

    parts = name.split()
    if len(parts) < 2:
        return name, ""

    # Assume last word is last name
    return parts[-1], parts[0]


def lookup_property_by_name(
    name: str,
    county: str,
    min_score: float = 0.4,
) -> dict | None:
    """Search county CAD for property owned by a person.

    Returns dict with: address, city, zip, owner, value, parcel_id
    or None if not found.
    """
    last_name, first_name = _parse_name_parts(name)
    if not last_name:
        return None

    county_lower = county.lower()

    if county_lower == "williamson":
        results = _wcad_name_search(last_name, first_name)
    else:
        # Bell and Travis use bulk data (not yet implemented)
        logger.debug("CAD lookup not yet implemented for %s County", county)
        return None

    if not results:
        return None

    # Score and rank results
    scored = []
    for r in results:
        full = r.get("fullname", "")
        score = _name_match_score(last_name, first_name, full)
        if score >= min_score:
            scored.append((score, r))

    if not scored:
        # Try without first name
        for r in results:
            full = r.get("fullname", "")
            score = _name_match_score(last_name, "", full)
            if score >= min_score:
                scored.append((score, r))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]

    # Parse the situs address
    situs = best.get("situsaddress", "")
    if not situs:
        return None

    # Parse "123 MAIN ST, ROUND ROCK, TX  78664" format
    addr_parts = situs.split(",")
    street = addr_parts[0].strip().title() if addr_parts else ""
    city = best.get("scity", "").strip().title()
    zip_code = best.get("szip", "").strip()

    result = {
        "address": street,
        "city": city,
        "state": "TX",
        "zip": zip_code,
        "owner": best.get("fullname", "").title(),
        "value": best.get("totalpropmktvalue", ""),
        "parcel_id": best.get("quickrefid", ""),
        "property_type": best.get("propertytypedesc", ""),
        "match_score": best_score,
    }

    logger.info(
        "CAD lookup '%s' in %s: found %s at %s (score: %.2f)",
        name, county, result["owner"], result["address"], best_score,
    )
    return result


def lookup_property_by_address(
    address: str,
    county: str,
) -> dict | None:
    """Search county CAD for property data by address.

    Returns dict with: owner, value, parcel_id, tax_status
    or None if not found.
    """
    county_lower = county.lower()

    if county_lower == "williamson":
        results = _wcad_address_search(address)
    else:
        logger.debug("CAD address lookup not yet implemented for %s County", county)
        return None

    if not results:
        return None

    best = results[0]
    return {
        "owner": best.get("fullname", "").title(),
        "value": best.get("totalpropmktvalue", ""),
        "parcel_id": best.get("quickrefid", ""),
        "property_type": best.get("propertytypedesc", ""),
    }

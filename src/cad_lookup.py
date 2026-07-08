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


def _wcad_parcel_search(parcel_id: str) -> list[dict]:
    """Search WCAD by parcel/quickref id (Williamson's R-number) via SODA API.

    Williamson records carry an `R`-number that matches WCAD's `quickrefid` —
    an exact key, so this is the reliable owner path when the address fuzz
    misses. Returns the owner + separated mailing address (mailing1/mcity/…)."""
    pid = (parcel_id or "").strip().replace("'", "")
    if not pid:
        return []
    where = f"quickrefid='{pid}'"
    select = (
        "propertyid,fullname,namelast,namefirst,situsaddress,scity,szip,"
        "mailing1,mcity,mstate,mzip,totalpropmktvalue,quickrefid,propertytypedesc"
    )
    url = f"{WCAD_API_BASE}/{WCAD_OWNER_DATASET}.json"
    try:
        resp = requests.get(
            url, params={"$where": where, "$select": select, "$limit": 5},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        return []
    except requests.RequestException as e:
        logger.warning("WCAD parcel search failed: %s", e)
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
    elif county_lower == "travis":
        from travis_tax_cache import search_by_name
        # Travis probate decedent names arrive as LAST FIRST MIDDLE format
        # (e.g. "Aynesworth Donald D"). _parse_name_parts assumes FIRST LAST
        # and gets it wrong, so also probe the first token as an index key.
        parts = [
            p for p in name.strip().split()
            if p.upper() not in {"JR", "SR", "II", "III", "IV", "ESQ"}
        ]
        try:
            results = list(search_by_name(last_name, first_name))
            if len(parts) >= 2 and parts[0].upper() != last_name.upper():
                results += search_by_name(parts[0], "")
        except Exception as e:
            logger.warning("Travis tax cache lookup failed: %s", e)
            return None
    elif county_lower == "bell":
        # TRIROLL: BellCAD has no live API, so the master cross-reference is
        # served from `bell_tax_cache` which lazy-loads the appraisal master
        # XLSX (full county) + delinquent overlay.
        from bell_tax_cache import search_by_name
        parts = [
            p for p in name.strip().split()
            if p.upper() not in {"JR", "SR", "II", "III", "IV", "ESQ"}
        ]
        try:
            results = list(search_by_name(last_name, first_name))
            if len(parts) >= 2 and parts[0].upper() != last_name.upper():
                results += search_by_name(parts[0], "")
        except Exception as e:
            logger.warning("Bell tax cache lookup failed: %s", e)
            return None
    else:
        logger.debug("CAD lookup not yet implemented for %s County", county)
        return None

    if not results:
        return None

    # ── Scoring ──
    # Travis: use ALL name tokens (not just last+first) and require at least
    # 2 non-noise tokens to overlap — this kills false positives like
    # "D & D Trust" matching "Aynesworth Donald D" on a single initial.
    # Other counties: original last+first scoring.
    scored: list[tuple[float, dict]] = []
    if county_lower in ("travis", "bell"):
        # Both Travis tax cache and Bell tax cache return records keyed off a
        # local roll where the API name format ("LAST, FIRST ETUX ...") doesn't
        # match the typical FIRST LAST search input. Use whole-token overlap
        # with a multi-token floor to avoid initials-only false positives.
        noise = {"&", "JR", "SR", "II", "III", "IV", "THE", "ESTATE", "OF", "TRUST", "LLC", "INC", "ETUX", "ETVIR", "ETAL"}
        search_tokens = {p.upper() for p in name.strip().split()} - noise
        multi_token = len(search_tokens) >= 2
        seen_ids: set[str] = set()
        for r in results:
            rid = r.get("quickrefid", "") or id(r)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            api_tokens = set(r.get("fullname", "").upper().replace(",", "").split()) - noise
            if not api_tokens or not search_tokens:
                continue
            overlap = search_tokens & api_tokens
            if multi_token and len(overlap) < 2:
                continue
            if not multi_token and not overlap:
                continue
            score = len(overlap) / max(len(search_tokens), len(api_tokens))
            if score >= min_score:
                scored.append((score, r))
    else:
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
        # Pristine raw all-caps owner — callers that want a clean display
        # name should pass this through their own ETAL strip + title-case
        # (Python's `.title()` lowercases LLC/III/JR — see property_lookup.py).
        "owner_raw": best.get("fullname", ""),
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
    zip_code: str = "",
) -> dict | None:
    """Search county CAD for property data by address.

    Returns dict with: owner, value, parcel_id, property_type, delinquent_total,
    years_delinquent, source — or None if not found.
    """
    county_lower = county.lower()

    if county_lower == "williamson":
        results = _wcad_address_search(address)
        if not results:
            return None
        best = results[0]
        return {
            "owner": best.get("fullname", "").title(),
            "owner_raw": best.get("fullname", ""),
            "value": best.get("totalpropmktvalue", ""),
            "parcel_id": best.get("quickrefid", ""),
            "property_type": best.get("propertytypedesc", ""),
            "delinquent_total": "",  # WCAD SODA datasets don't carry tax
            "years_delinquent": "",
            "source": "wcad_soda",
        }

    if county_lower == "travis":
        try:
            from travis_tax_cache import search_by_address
            rec = search_by_address(address, zip_code)
        except Exception as e:
            logger.warning("Travis CAD address lookup failed: %s", e)
            return None
        if not rec:
            return None
        return {
            "owner": rec.get("fullname", "").title(),
            "owner_raw": rec.get("fullname", ""),
            "value": rec.get("totalpropmktvalue", ""),
            "parcel_id": rec.get("quickrefid", ""),
            "property_type": rec.get("propertytypedesc", ""),
            "delinquent_total": rec.get("delinquent_total", ""),
            "years_delinquent": rec.get("years_delinquent", ""),
            "source": rec.get("source", "travis_tax_cache"),
        }

    if county_lower == "bell":
        # TRIROLL: Bell uses the same local-cache shape as Travis.
        try:
            from bell_tax_cache import search_by_address
            rec = search_by_address(address, zip_code)
        except Exception as e:
            logger.warning("Bell CAD address lookup failed: %s", e)
            return None
        if not rec:
            return None
        return {
            "owner": rec.get("fullname", "").title(),
            "owner_raw": rec.get("fullname", ""),
            "value": rec.get("totalpropmktvalue", ""),
            "parcel_id": rec.get("quickrefid", ""),
            "property_type": rec.get("propertytypedesc", ""),
            "delinquent_total": rec.get("delinquent_total", ""),
            "years_delinquent": rec.get("years_delinquent", ""),
            "source": rec.get("source", "bell_tax_cache"),
        }

    logger.debug("CAD address lookup not yet implemented for %s County", county)
    return None


def lookup_property_by_parcel(parcel_id: str, county: str) -> dict | None:
    """Search county CAD for property data by parcel/geo id (exact match).

    High-leverage owner path for records that arrive with a parcel but no owner
    (Austin code-enforcement, absentee LLC owners). Exact-match avoids the
    address-normalization fuzz.

    Tries the tagged county first, then falls back to the OTHER counties: an
    address can sit on a neighbouring county's roll (Austin's ETJ spills into
    Williamson, so many "Travis" Austin records are physically Williamson), and
    an `R`-number is the WCAD/TCAD property-ID format shared across rolls.
    Returns the standard result dict, or None.
    """
    if not parcel_id or not str(parcel_id).strip():
        return None
    cl = county.lower()
    result = _parcel_lookup_one(parcel_id, cl)
    if result:
        return result
    # Cross-county fallback — order Williamson first (the common Austin-ETJ /
    # R-number overlap), then the remaining local caches.
    for other in ("williamson", "travis", "bell"):
        if other == cl:
            continue
        result = _parcel_lookup_one(parcel_id, other)
        if result:
            return result
    return None


def _parcel_lookup_one(parcel_id: str, county: str) -> dict | None:
    """Single-county parcel→owner lookup (Travis/Bell local caches, Williamson
    live WCAD). Returns the standard result dict or None."""
    if not parcel_id or not str(parcel_id).strip():
        return None
    cl = county.lower()
    try:
        if cl == "travis":
            from travis_tax_cache import search_by_parcel
            rec = search_by_parcel(parcel_id)
            source = "travis_parcel"
        elif cl == "bell":
            from bell_tax_cache import search_by_parcel
            rec = search_by_parcel(parcel_id)
            source = "bell_parcel"
        elif cl == "williamson":
            results = _wcad_parcel_search(parcel_id)
            w = results[0] if results else None
            rec = None
            if w:
                rec = {
                    "fullname": w.get("fullname", ""),
                    "quickrefid": w.get("quickrefid", ""),
                    "totalpropmktvalue": w.get("totalpropmktvalue", ""),
                    "propertytypedesc": w.get("propertytypedesc", ""),
                    "mailing": w.get("mailing1", ""),
                    "scity": (w.get("mcity") or "").upper(),
                    "sstate": (w.get("mstate") or "TX").upper(),
                    "szip": (w.get("mzip") or "")[:5],
                }
            source = "wcad_parcel"
        else:
            return None
    except Exception as e:
        logger.warning("%s CAD parcel lookup failed: %s", county, e)
        return None
    if not rec:
        return None
    # Owner mailing: Bell stores a combined mailing string in "mailing"; Travis
    # uses the (current-roll) mailing as its situs. Fall back to whichever exists.
    mail_street = (rec.get("mailing") or rec.get("situsaddress") or "").strip()
    return {
        "owner": rec.get("fullname", "").title(),
        "owner_raw": rec.get("fullname", ""),
        "value": rec.get("totalpropmktvalue", ""),
        "parcel_id": rec.get("quickrefid", ""),
        "property_type": rec.get("propertytypedesc", ""),
        "delinquent_total": rec.get("delinquent_total", ""),
        "years_delinquent": rec.get("years_delinquent", ""),
        # Owner's tax-roll mailing address — where an absentee/LLC owner actually
        # receives mail. Filled only when the owner mailing is otherwise blank.
        "mail_street": mail_street,
        "mail_city": rec.get("scity", ""),
        "mail_state": rec.get("sstate", "TX"),
        "mail_zip": rec.get("szip", ""),
        "source": source,
    }

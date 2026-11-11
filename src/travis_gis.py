"""Travis County parcel fabric (TCAD GIS) — the TRUE situs address for a parcel.

Why this exists (2026-08-29): the Travis tax cache is built from two Tax Office
CSVs, and the CURRENT roll (~471K parcels) has NO situs column — only the
owner's MAILING address. For years the cache stored that mailing address in
the record slot named ``situsaddress`` on the theory that most owners live at
their property. That guess is wrong for every absentee owner, and it shipped
an LLC's PO Box ("P.O. Box 41539") and a debtor's office ("500 W 2nd St Ste
1900") as PROPERTY addresses on lien lists. The cache no longer does that
(current-roll records now carry a blank situs and explicit ``mailing`` keys);
this module supplies the real situs from the parcel instead.

Source: the Travis County GIS parcel layer already proven by the fire-damage
scraper (``TCAD_public/MapServer/0``, ~386K parcels). Attribute queries by
``geo_id`` work (verified live 2026-08-29 — the PO-Box LLC above actually owns
11311 Wedgewood Dr 78753). Fields: geo_id, PROP_ID, situs_num,
situs_street_prefx, situs_street, situs_street_suffix, situs_zip,
situs_address ("9500 E U S HY 290 78724" — carries a trailing ZIP token),
situs_city (usually null — city is derived from the ZIP instead).

The GIS ``geo_id`` is the same 10-digit key as ``travis_tax_cache``'s parcel
index; TCAD's 14-digit account/quickrefid reduces to it via
``travis_tax_cache._normalize_parcel``.
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

TCAD_PARCEL_URL = (
    "https://gis.traviscountytx.gov/server1/rest/services/"
    "Boundaries_and_Jurisdictions/TCAD_public/MapServer/0/query"
)
GIS_DELAY = 0.3       # seconds between calls (same courtesy as fire_damage)
GIS_TIMEOUT = 45
_OUT_FIELDS = (
    "geo_id,PROP_ID,situs_num,situs_street_prefx,situs_street,"
    "situs_street_suffix,situs_zip,situs_address,situs_city"
)

# geo_id -> result dict, or None when the parcel exists but has no situs
# (vacant land / unaddressed tract). Transient failures are NOT memoized.
_memo: dict[str, dict | None] = {}
_last_call = 0.0


def normalize_geo_id(parcel_id: str) -> str:
    """Reduce a TCAD account / quickrefid / geo_id to the 10-digit geo key."""
    try:
        from travis_tax_cache import _normalize_parcel
        geo = _normalize_parcel(parcel_id or "")
    except Exception:
        geo = "".join(ch for ch in str(parcel_id or "") if ch.isdigit())[:10]
    return geo if geo and geo.isdigit() and len(geo) == 10 else ""


def _throttle() -> None:
    global _last_call
    wait = GIS_DELAY - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _situs_from_feature(attrs: dict) -> dict | None:
    num = str(attrs.get("situs_num") or "").strip()
    zip5 = str(attrs.get("situs_zip") or "").strip()[:5]
    full = str(attrs.get("situs_address") or "").strip()
    if zip5 and full.endswith(zip5):
        full = full[: -len(zip5)].strip()
    if not full:
        parts = [num, attrs.get("situs_street_prefx"), attrs.get("situs_street"),
                 attrs.get("situs_street_suffix")]
        full = " ".join(str(p).strip() for p in parts if p and str(p).strip())
    # A parcel with no house number is unaddressed land — no situs to give.
    if not num or not full:
        return None
    city = str(attrs.get("situs_city") or "").strip()
    if not city and zip5:
        try:
            from scrapers.travis_texdel_cleaner import city_for_zip
            city = city_for_zip(zip5)
        except Exception:
            city = "Austin"
    return {
        "geo_id": str(attrs.get("geo_id") or ""),
        "prop_id": str(attrs.get("PROP_ID") or ""),
        "address": full.title(),
        "city": (city or "Austin").title(),
        "state": "TX",
        "zip": zip5,
        "source": "tcad_gis",
    }


# address -> geo_id (or None for a confirmed miss)
_situs_memo: dict[tuple[str, str], str | None] = {}
_DIRECTIONALS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST"}
_STREET_SUFFIXES = {
    "ST", "STREET", "DR", "DRIVE", "RD", "ROAD", "LN", "LANE", "AVE", "AVENUE", "BLVD",
    "CT", "COURT", "CV", "COVE", "CIR", "CIRCLE", "LOOP", "LP", "PL", "PLACE", "WAY",
    "PKWY", "PARKWAY", "TRL", "TRAIL", "TRCE", "TRACE", "PASS", "BND", "BEND", "XING",
    "HWY", "HY", "RUN", "PATH", "TER", "TERRACE", "SQ", "PT", "POINT", "HOLW", "VW",
}


def geo_id_by_situs(address: str, zip5: str = "") -> str | None:
    """Find the TCAD parcel (10-digit geo_id) whose SITUS is ``address``.

    The reverse of situs_by_geo_id, added 2026-11-11 for records that arrive
    with a street address but no parcel id (Travis foreclosures, name-backfilled
    liens): the tax-cache ADDRESS index is keyed by owner MAILING address, so it
    only finds owner-occupants and can return a tenant's own parcel elsewhere.
    The parcel fabric's situs is the property itself.

    Query = house number + first significant street word (directional and
    suffix stripped), filtered to the ZIP. Returns the geo_id only when exactly
    one parcel matches; ambiguous or missing -> None. Never raises.
    """
    import re as _re
    m = _re.match(r"^\s*(\d+)\s+(.+)$", address or "")
    if not m:
        return None
    num = m.group(1)
    words = [w for w in _re.sub(r"[^A-Z0-9 ]", " ", m.group(2).upper()).split()]
    sig = [w for w in words if w not in _DIRECTIONALS and w not in _STREET_SUFFIXES]
    stem = (sig or words or [""])[0]
    stem = _re.sub(r"[^A-Z0-9]", "", stem)
    z5 = (zip5 or "")[:5]
    if not stem:
        return None
    key = (f"{num} {' '.join(words)}", z5)
    if key in _situs_memo:
        return _situs_memo[key]
    try:
        _throttle()
        resp = requests.get(
            TCAD_PARCEL_URL,
            params={
                # num digits-only and stem alphanumeric-only (validated above).
                "where": f"situs_num='{num}' AND situs_street LIKE '{stem}%'",
                "outFields": _OUT_FIELDS,
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=GIS_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("TCAD GIS situs->parcel lookup failed for %r: %s", address, e)
        return None
    if data.get("error"):
        logger.warning("TCAD GIS situs->parcel error for %r: %s", address, data["error"])
        return None
    geos = set()
    for feat in data.get("features") or []:
        a = feat.get("attributes") or {}
        fz = str(a.get("situs_zip") or "")[:5]
        if z5 and fz and fz != z5:
            continue
        g = normalize_geo_id(str(a.get("geo_id") or ""))
        if g:
            geos.add(g)
    result = geos.pop() if len(geos) == 1 else None
    _situs_memo[key] = result
    return result


def situs_by_geo_id(parcel_id: str) -> dict | None:
    """Return {address, city, state, zip, geo_id, prop_id, source} for a Travis
    parcel, or None when the parcel is unknown / has no situs / GIS is down.

    Never raises. Memoizes hits and confirmed misses; a network error is
    logged and returns None without being cached so a later call can retry.
    """
    geo = normalize_geo_id(parcel_id)
    if not geo:
        return None
    if geo in _memo:
        return _memo[geo]
    try:
        _throttle()
        resp = requests.get(
            TCAD_PARCEL_URL,
            params={
                "where": f"geo_id='{geo}'",   # digits only — validated above
                "outFields": _OUT_FIELDS,
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=GIS_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("TCAD GIS situs lookup failed for geo_id %s: %s", geo, e)
        return None
    if data.get("error"):
        logger.warning("TCAD GIS situs lookup error for geo_id %s: %s", geo, data["error"])
        return None
    result = None
    for feat in data.get("features") or []:
        result = _situs_from_feature(feat.get("attributes") or {})
        if result:
            break
    _memo[geo] = result
    return result

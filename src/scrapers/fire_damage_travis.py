"""Travis County fire-damage scraper — Austin/Travis CTECC Real-Time Fire feed.

Structure fires are the earliest possible worst-condition signal: a burned
house typically sits 1-3 YEARS before a code case opens, and longer before any
condemnation docket. This scraper catches them within a day of the dispatch.

Source dataset: "Real-Time Fire Incidents" (id wpu4-x69d)
  https://datahub.austintexas.gov/dataset/Real-Time-Fire-Incidents/wpu4-x69d
API endpoint:   https://datahub.austintexas.gov/resource/wpu4-x69d.json  (SoQL)
Feed:           CTECC (Combined Transportation, Emergency & Communications
                Center) — Austin FD + Travis County ESDs, refreshed every
                5 minutes, rolling ~12 months of history (~38K FIRE rows/yr).

Why THIS dataset (verified live 2026-07-22):
  * "AFD Fire Incidents 2023-2025" (v5hh-nyr8) is only refreshed ~quarterly
    (last: 2026-04-20) and carries NO street address (lat/long only) — useless
    for a daily pull.
  * wpu4-x69d updates in real time AND carries a street `address` field
    ("151 Los Fresnos Dr"), so most rows arrive with a usable address.

Owner/parcel resolution — the load-bearing part (hard-won 2026-07-23):
  * The feed has no city/ZIP/parcel. A blank ZIP is fatal downstream: CAD
    owner resolution (enrichment Step 5) runs BEFORE Smarty (Step 6), and
    travis_tax_cache.search_by_address needs street+ZIP — and even with a
    ZIP, that index only keys owner-occupied situs (absentee-owned parcels
    are keyed by the owner's MAILING address, often out of county).
  * Fix: resolve each fire's coordinates to the TCAD PARCEL via the Travis
    County GIS parcel layer (TCAD_public/MapServer/0, 386K parcels). The
    geo_id it returns IS the 10-digit key of travis_tax_cache's parcel index
    (which covers EVERY owner incl. absentee), so Step 5's parcel-first path
    resolves the owner exactly — no address fuzz. The layer's situs also
    supplies the authoritative ZIP (reverse-geocoded ZIPs are routinely
    wrong: Nominatim returns PO-box ZIPs like 78715 for 78745 streets).
  * GIS quirks: the ArcGIS server SILENTLY returns zero features for
    inSR=4326 input — points must be sent in the layer's native
    EPSG:2277 (TX State Plane Central, US-ft), projected inline below
    (pure-python Lambert, verified within ~2.5 ft of the server's own
    GeometryServer). And CTECC dispatch points are snapped to the STREET
    CENTERLINE — right-of-way, inside no parcel — so the query uses a
    200 ft buffer and picks the candidate whose situs_num matches the
    feed address's house number.
  * Nominatim reverse geocoding (1.1s/record) survives only as the fallback
    for rows the GIS can't place (edge-of-county subdivisions the parcel
    layer lags, intersection-only addresses).

Problem-type taxonomy (issue_reported, counts from the live feed):
  BOX -Structure Fire (606/yr) and BOXL- Structure Fire (244/yr) are the
  single-family-scale structure fires we want (~2-3/day combined).
  BOXMID/BOXHI (mid/hi-rise, 34/yr) are multifamily/commercial and excluded
  by default; ELEC - Electrical Fire is mostly poles/lines, excluded.
  Override the kept set via FIRE_DAMAGE_PROBLEMS (comma-separated, exact
  issue_reported strings).

Each kept incident becomes NoticeData(notice_type="fire_damage",
county="Travis"). owner_name is intentionally blank — filled by the CAD
owner resolution in enrichment Step 5 (parcel-first when the GIS matched,
address+ZIP otherwise), exactly like code_violation records. Apartment/
commercial/entity-owned hits are dropped by the existing condo, commercial,
and entity filters downstream.

Cross-run dedup: rows have no ?ID= URL, so the seen-ids key is parcel:<geo_id>
when the GIS matched, else the composite addr:fire_damage|travis|<address>|…
— both stable because keys are snapshotted at scrape time.
"""

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta

import requests

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

DATASET_ID = "wpu4-x69d"
API_URL = f"https://datahub.austintexas.gov/resource/{DATASET_ID}.json"
DATASET_URL = (
    f"https://datahub.austintexas.gov/dataset/Real-Time-Fire-Incidents/{DATASET_ID}"
)

# Default structure-fire dispatch types (exact issue_reported strings — note
# the feed's own inconsistent spacing around the hyphens).
DEFAULT_PROBLEMS = (
    "BOX -Structure Fire",
    "BOXL- Structure Fire",
)

PAGE_SIZE = 1000
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
HISTORICAL_DAYS = 365      # the feed only holds ~12 months anyway
DAILY_DEFAULT_DAYS = 30    # daily mode with no since_date: last 30 days

# A usable property address starts with a house number ("151 Los Fresnos Dr").
_HOUSE_NUMBER_RE = re.compile(r"^(\d+)\s+\S")

# ── TCAD parcel GIS (Travis County GIS) ───────────────────────────────
TCAD_PARCEL_URL = (
    "https://gis.traviscountytx.gov/server1/rest/services/"
    "Boundaries_and_Jurisdictions/TCAD_public/MapServer/0/query"
)
PARCEL_SEARCH_FT = 200     # buffer around the road-snapped dispatch point
GIS_DELAY = 0.3            # polite pacing between parcel queries
GIS_TIMEOUT = 45

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_DELAY = 1.1      # Nominatim rate limit: 1 req/sec


def _problems() -> tuple[str, ...]:
    raw = os.getenv("FIRE_DAMAGE_PROBLEMS", "").strip()
    if raw:
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return DEFAULT_PROBLEMS


def _quote_list(values) -> str:
    """SoQL in() list — single quotes doubled per SoQL escaping."""
    return ",".join("'{}'".format(str(v).replace("'", "''")) for v in values)


# ── EPSG:2277 projection (WGS84 → NAD83 / Texas Central, US survey ft) ─
# Lambert Conformal Conic 2SP (Snyder). The GIS server silently returns zero
# features for inSR=4326 geometry, so points must be projected client-side.
# Verified within ~2.5 ft of the server's own GeometryServer/project result
# (the WGS84↔NAD83 datum shift) — negligible vs the 200 ft search buffer.
_GRS80_A = 6378137.0
_GRS80_F = 1 / 298.257222101
_E2 = 2 * _GRS80_F - _GRS80_F ** 2
_E = math.sqrt(_E2)
_LAT1 = math.radians(30 + 7 / 60)       # standard parallel 1
_LAT2 = math.radians(31 + 53 / 60)      # standard parallel 2
_LAT0 = math.radians(29 + 40 / 60)      # latitude of origin
_LON0 = math.radians(-(100 + 20 / 60))  # central meridian
_US_FT = 0.30480060960121924            # US survey foot in meters
_FE = 700000.0 / _US_FT                 # false easting (ftUS)
_FN = 3000000.0 / _US_FT                # false northing (ftUS)


def _lcc_m(lat: float) -> float:
    return math.cos(lat) / math.sqrt(1 - _E2 * math.sin(lat) ** 2)


def _lcc_t(lat: float) -> float:
    return math.tan(math.pi / 4 - lat / 2) / (
        (1 - _E * math.sin(lat)) / (1 + _E * math.sin(lat))
    ) ** (_E / 2)


_LCC_N = (math.log(_lcc_m(_LAT1)) - math.log(_lcc_m(_LAT2))) / (
    math.log(_lcc_t(_LAT1)) - math.log(_lcc_t(_LAT2))
)
_LCC_F = _lcc_m(_LAT1) / (_LCC_N * _lcc_t(_LAT1) ** _LCC_N)
_LCC_RHO0 = _GRS80_A * _LCC_F * _lcc_t(_LAT0) ** _LCC_N


def _to_state_plane(lon: float, lat: float) -> tuple[float, float]:
    """Project WGS84 lon/lat to EPSG:2277 x/y in US survey feet."""
    rho = _GRS80_A * _LCC_F * _lcc_t(math.radians(lat)) ** _LCC_N
    theta = _LCC_N * (math.radians(lon) - _LON0)
    x = rho * math.sin(theta)
    y = _LCC_RHO0 - rho * math.cos(theta)
    return (x / _US_FT + _FE, y / _US_FT + _FN)


def _parcel_lookup(lat: str, lon: str, house_num: str) -> dict:
    """Resolve a fire's coordinates to its TCAD parcel.

    Buffered point query against the parcel fabric; a candidate is accepted
    when its situs_num equals the feed address's house number, or — for rows
    with no house number (intersections) — when the buffer holds exactly one
    parcel. Returns {geo_id, situs_address, situs_zip} or {}.
    """
    try:
        x, y = _to_state_plane(float(lon), float(lat))
    except (TypeError, ValueError):
        return {}
    params = {
        "geometry": json.dumps(
            {"x": x, "y": y, "spatialReference": {"wkid": 102739}}
        ),
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": str(PARCEL_SEARCH_FT),
        "units": "esriSRUnit_Foot",
        "outFields": "geo_id,situs_num,situs_address,situs_zip",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = requests.get(TCAD_PARCEL_URL, params=params, timeout=GIS_TIMEOUT)
        resp.raise_for_status()
        feats = resp.json().get("features", [])
    except Exception as e:
        logger.debug("fire_damage: parcel GIS query failed: %s", e)
        return {}

    atts = [f.get("attributes", {}) for f in feats]
    if house_num:
        matches = [
            a for a in atts
            if str(a.get("situs_num") or "").strip() == house_num
            and (a.get("geo_id") or "").strip()
        ]
    else:
        matches = [a for a in atts if (a.get("geo_id") or "").strip()]
        if len(matches) != 1:
            return {}
    if not matches:
        return {}
    a = matches[0]
    return {
        "geo_id": (a.get("geo_id") or "").strip(),
        "situs_address": (a.get("situs_address") or "").strip(),
        "situs_zip": (a.get("situs_zip") or "").strip()[:5],
    }


def _reverse_geocode(lat: str, lon: str) -> dict:
    """Nominatim reverse geocode → {street, city, postcode} (any may be "").

    Fallback path only — used when the parcel GIS can't place the fire.
    Nominatim postcodes are unreliable (PO-box ZIPs), but a fuzzy ZIP still
    beats none: the street-fallback in travis_tax_cache absorbs ZIP errors.
    """
    out = {"street": "", "city": "", "postcode": ""}
    try:
        time.sleep(NOMINATIM_DELAY)
        resp = requests.get(
            NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
            headers={"User-Agent": "SiftStack/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return out
        addr = resp.json().get("address", {})
    except Exception:
        return out
    house = (addr.get("house_number") or "").strip()
    road = (addr.get("road") or "").strip()
    if house and road:
        out["street"] = f"{house} {road}"
    out["city"] = (
        addr.get("city") or addr.get("town") or addr.get("village")
        or addr.get("hamlet") or ""
    ).strip()
    out["postcode"] = (addr.get("postcode") or "").strip()[:5]
    return out


def _row_to_notice(row: dict) -> NoticeData | None:
    """Map one CTECC feed row to a NoticeData, or None if no property address."""
    address = (row.get("address") or "").strip()
    lat = str(row.get("latitude") or "").strip()
    lon = str(row.get("longitude") or "").strip()

    m = _HOUSE_NUMBER_RE.match(address)
    house_num = m.group(1) if m else ""

    # Primary: coordinates → TCAD parcel (exact owner path + authoritative ZIP).
    gis: dict = {}
    if lat and lon:
        time.sleep(GIS_DELAY)
        gis = _parcel_lookup(lat, lon, house_num)

    city = ""
    zip5 = gis.get("situs_zip", "")
    if gis and not house_num:
        # Intersection-only feed address; adopt the matched parcel's situs
        # ("2111 SHILOH DR 78745" — strip the trailing ZIP token).
        situs = gis["situs_address"]
        if zip5 and situs.endswith(zip5):
            situs = situs[: -len(zip5)].strip()
        address = situs

    if not gis:
        # Fallback: Nominatim for street (if needed) + city/ZIP.
        geo = _reverse_geocode(lat, lon) if (lat and lon) else None
        if geo:
            if not house_num:
                address = geo["street"]
            city = geo["city"]
            zip5 = geo["postcode"]

    if not _HOUSE_NUMBER_RE.match(address or ""):
        logger.debug(
            "fire_damage: dropping incident with no property address: %r",
            row.get("address"),
        )
        return None

    problem = (row.get("issue_reported") or "").strip()
    status = (row.get("traffic_report_status") or "").strip()
    incident_id = (row.get("traffic_report_id") or "").strip()
    reported = (row.get("published_date") or "").strip()

    raw_bits = [b for b in (
        problem,
        f"Reported: {reported[:16].replace('T', ' ')}" if reported else "",
        f"Status: {status}" if status else "",
        f"Incident: {incident_id}" if incident_id else "",
    ) if b]

    return NoticeData(
        date_added=reported[:10],
        address=address.title(),
        city=city.title(),
        state="TX",
        zip=zip5,
        owner_name="",  # filled by TCAD owner resolution (enrichment Step 5)
        notice_type="fire_damage",
        county="Travis",
        source_url=DATASET_URL,
        raw_text=" | ".join(raw_bits),
        latitude=lat,
        longitude=lon,
        parcel_id=gis.get("geo_id", ""),
        violation_description=problem or "Structure Fire",
        case_status=status,
        case_id=incident_id,
    )


@register("Travis", "fire_damage")
class TravisFireDamageScraper:
    """Structure-fire incidents via the CTECC Real-Time Fire Socrata feed."""

    def _since_iso(self, mode: str, since_date: str | None) -> str:
        if mode == "historical":
            cutoff = datetime.now() - timedelta(days=HISTORICAL_DAYS)
        elif since_date:
            try:
                cutoff = datetime.strptime(since_date, "%Y-%m-%d")
            except ValueError:
                logger.warning(
                    "Invalid since_date %r — defaulting to last %d days",
                    since_date, DAILY_DEFAULT_DAYS,
                )
                cutoff = datetime.now() - timedelta(days=DAILY_DEFAULT_DAYS)
        else:
            cutoff = datetime.now() - timedelta(days=DAILY_DEFAULT_DAYS)
        return cutoff.strftime("%Y-%m-%dT00:00:00")

    def _fetch_page(self, where: str, offset: int) -> list[dict]:
        params = {
            "$where": where,
            "$order": "published_date DESC",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        }
        headers = {"Accept": "application/json"}
        token = os.getenv("SOCRATA_APP_TOKEN")
        if token:
            headers["X-App-Token"] = token

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    API_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as e:
                logger.warning(
                    "CTECC fire feed fetch failed (offset=%d, attempt %d/%d): %s",
                    offset, attempt, MAX_RETRIES, e,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(2 * attempt)
        return []

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        since_iso = self._since_iso(mode, since_date)
        problems = _problems()
        where = (
            f"agency='FIRE' AND issue_reported in({_quote_list(problems)}) "
            f"AND published_date >= '{since_iso}'"
        )

        notices: list[NoticeData] = []
        dropped = 0
        offset = 0
        while True:
            batch = self._fetch_page(where, offset)
            if not batch:
                break
            for row in batch:
                notice = _row_to_notice(row)
                if notice is None:
                    dropped += 1
                    continue
                notices.append(notice)
                if max_notices and len(notices) >= max_notices:
                    logger.info("fire_damage: hit max_notices=%d cap", max_notices)
                    return notices
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        with_parcel = sum(1 for n in notices if n.parcel_id)
        logger.info(
            "Travis fire damage: %d structure fires kept (%d parcel-resolved), "
            "%d without a property address dropped (mode=%s, reported >= %s, types=%s)",
            len(notices), with_parcel, dropped, mode, since_iso[:10],
            "/".join(problems),
        )
        return notices

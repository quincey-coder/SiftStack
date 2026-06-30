"""Travis County (City of Austin) code-enforcement scraper — Socrata SODA API.

Pulls OPEN Austin Code Department complaint cases from the City of Austin
Open Data portal. No login, no Playwright, no Dropbox — a direct REST/SoQL
query against a public Socrata dataset, in the same spirit as the WCAD SODA
lookups in cad_lookup.py.

Source dataset: "Austin Code Complaint Cases" (id 6wtj-zbtb)
  https://data.austintexas.gov/Public-Safety/Austin-Code-Complaint-Cases/6wtj-zbtb
API endpoint:   https://data.austintexas.gov/resource/6wtj-zbtb.json   (SoQL)
Refresh:        daily, by Austin Development Services / Austin Code
Coverage:       City of Austin jurisdiction — the bulk of Travis County code
                activity (~3.6K open cases at any time). Smaller Travis cities
                and unincorporated areas are NOT in this dataset; they would
                need separate sources (open-records, like Bell/Williamson).

Each open case becomes NoticeData(notice_type="code_violation", county="Travis").
owner_name is intentionally blank: the dataset is address/parcel keyed, so the
enrichment pipeline fills the owner via TCAD parcel/address lookup. The Austin
`parcelid` is passed straight through to make that lookup exact.
"""

import logging
import os
import time
from datetime import datetime, timedelta

import requests

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

DATASET_ID = "6wtj-zbtb"
API_URL = f"https://data.austintexas.gov/resource/{DATASET_ID}.json"
DATASET_URL = (
    "https://data.austintexas.gov/Public-Safety/"
    "Austin-Code-Complaint-Cases/6wtj-zbtb"
)

# Open/actionable statuses worth marketing to. "Closed" (~79K rows) is excluded.
OPEN_STATUSES = ("Active", "Pending")
PAGE_SIZE = 1000           # Socrata default/max per page without app token tuning
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
HISTORICAL_DAYS = 365      # historical mode: open cases opened within last 12 mo
DAILY_DEFAULT_DAYS = 30    # daily mode with no since_date: last 30 days


def _quote_list(values) -> str:
    """SoQL in() list: ('Active','Pending')."""
    return ",".join(f"'{v}'" for v in values)


def _clean_zip(raw) -> str:
    """Austin zip_code is a number column; normalize to a 5-digit string."""
    if raw in (None, ""):
        return ""
    s = str(raw).split(".")[0].strip()
    return s[:5] if s.isdigit() else ""


def _row_to_notice(row: dict) -> NoticeData | None:
    """Map one Socrata row to a NoticeData, or None if it has no usable address."""
    house = (row.get("house_number") or "").strip()
    street = (row.get("street_name") or "").strip()
    if house or street:
        address = f"{house} {street}".strip().title()
    else:
        # ADDRESS_LONG may carry a "Unit NNN" suffix; keep the base street line.
        address = (row.get("address") or "").split(" Unit ")[0].strip().title()
    if not address:
        return None

    desc = (row.get("description") or "").strip()
    case_type = (row.get("case_type") or "").strip()
    status = (row.get("status") or "").strip()
    case_id = (row.get("case_id") or "").strip()

    loc = row.get("location") or {}
    lat = str(row.get("latitude") or loc.get("latitude") or "").strip()
    lon = str(row.get("longitude") or loc.get("longitude") or "").strip()

    raw_bits = [b for b in (
        case_type,
        desc,
        f"Status: {status}" if status else "",
        f"Case: {case_id}" if case_id else "",
    ) if b]

    # Socrata url-type fields arrive as {"url": "..."} dicts, not strings.
    link = row.get("violationcaselink")
    if isinstance(link, dict):
        link = link.get("url", "")
    link = (link or "").strip()

    return NoticeData(
        date_added=(row.get("opened_date") or "")[:10],
        address=address,
        city=(row.get("city") or "Austin").title(),
        state="TX",
        zip=_clean_zip(row.get("zip_code")),
        owner_name="",  # filled by TCAD enrichment via parcel_id / address
        notice_type="code_violation",
        county="Travis",
        source_url=link or DATASET_URL,
        raw_text=" | ".join(raw_bits),
        latitude=lat,
        longitude=lon,
        parcel_id=(row.get("parcelid") or "").strip(),
        violation_description=desc or case_type,
        case_status=status,
    )


@register("Travis", "code_violation")
class TravisCodeEnforcementScraper:
    """Open Austin Code complaint cases via the public Socrata SODA API."""

    def _since_iso(self, mode: str, since_date: str | None) -> str:
        """Compute the opened_date lower bound as a SoQL floating timestamp."""
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
            "$order": "opened_date DESC",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        }
        headers = {"Accept": "application/json"}
        # Optional Socrata app token lifts the anonymous throttle if provided.
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
                    "Austin SODA fetch failed (offset=%d, attempt %d/%d): %s",
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
        where = f"status in({_quote_list(OPEN_STATUSES)}) AND opened_date >= '{since_iso}'"

        notices: list[NoticeData] = []
        offset = 0
        while True:
            batch = self._fetch_page(where, offset)
            if not batch:
                break
            for row in batch:
                notice = _row_to_notice(row)
                if notice is None:
                    continue
                notices.append(notice)
                if max_notices and len(notices) >= max_notices:
                    logger.info(
                        "Austin code enforcement: hit max_notices=%d cap", max_notices,
                    )
                    return notices
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        logger.info(
            "Austin code enforcement: %d open cases (mode=%s, opened >= %s)",
            len(notices), mode, since_iso[:10],
        )
        # Keep only neglect/distress violation types (closed already excluded
        # at query time). Shared with the Bell/Williamson open-records ingest.
        from violation_filter import filter_code_violations
        return filter_code_violations(notices)

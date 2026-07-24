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
from scrapers import code_enforcement_state as ce_state

logger = logging.getLogger(__name__)

# Populated at end of each scrape() for main.py to collect (mirrors the
# tax-delinquent LAST_RUN_* globals). LAST_RUN_RESOLVED holds "Code Violation
# Resolved" NoticeData for cases that closed/complied since the prior run.
LAST_RUN_DIFF: dict | None = None
LAST_RUN_RESOLVED: list | None = None
LAST_RUN_REPORT_PATH = None  # Path to this run's diff JSON (forensics upload)

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


def _addr_key(address: str, zip_code: str) -> str:
    """Normalized (address, zip) match key for cross-referencing open vs resolved.

    Mirrors how the CRM matches records (by property address). Lower/stripped
    address + 5-digit zip so a resolved record can be checked against the set of
    still-open addresses regardless of case.
    """
    a = (address or "").strip().lower()
    z = _clean_zip(zip_code)
    return f"{a}|{z}" if a else ""


def _open_address_key(row: dict) -> str:
    """Build the same address key from a raw Socrata row as `_row_to_notice`."""
    house = (row.get("house_number") or "").strip()
    street = (row.get("street_name") or "").strip()
    if house or street:
        address = f"{house} {street}".strip().title()
    else:
        address = (row.get("address") or "").split(" Unit ")[0].strip().title()
    return _addr_key(address, row.get("zip_code"))


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
        case_id=case_id,
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

    def _fetch_page(self, where: str, offset: int, select: str | None = None) -> list[dict]:
        params = {
            "$where": where,
            "$order": "opened_date DESC",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        }
        if select:
            params["$select"] = select
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
        global LAST_RUN_DIFF, LAST_RUN_RESOLVED
        LAST_RUN_DIFF = None
        LAST_RUN_RESOLVED = None

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
        kept = filter_code_violations(notices)

        # Resolution tracking: diff the FULL current open-case set against the
        # prior run to surface cases that closed/complied, and rehydrate the
        # ones we previously uploaded into "Code Violation Resolved" records.
        # Skip when capped (partial pull) — the diff needs a complete snapshot.
        if not max_notices:
            try:
                self._run_resolution_diff(kept)
            except Exception as e:  # never let bookkeeping break the scrape
                logger.warning("Code-enforcement resolution diff failed: %s", e)

        return kept

    # ── Resolution tracking (full open-set diff) ─────────────────────
    def _fetch_all_open_cases(self) -> tuple[set[str], set[str]]:
        """Full snapshot of the currently-open set (no opened_date floor).

        The daily scrape query is windowed to recently-opened cases, so it can't
        be diffed for resolution. This pulls the complete Active/Pending set
        (~3.6K cases) and returns BOTH:
          - every open `case_id` (for the run-over-run resolution diff), and
          - a set of normalized property-address keys currently open.

        The address set guards the resolved/"Code Violation Resolved" tagging:
        a property can churn MULTIPLE code cases over time, so an OLD case can
        close (dropping its case_id) while a DIFFERENT case at the same address
        is still open. Since the CRM cleanup matches by ADDRESS, tagging that
        property "Resolved" would wrongly scope an actively-violating property
        out of marketing — so `_run_resolution_diff` suppresses any resolved
        record whose address is still in this set.
        """
        where = f"status in({_quote_list(OPEN_STATUSES)})"
        ids: set[str] = set()
        open_addr_keys: set[str] = set()
        offset = 0
        while True:
            batch = self._fetch_page(
                where, offset,
                select="case_id,house_number,street_name,address,zip_code",
            )
            if not batch:
                break
            for row in batch:
                cid = (row.get("case_id") or "").strip()
                if cid:
                    ids.add(cid)
                key = _open_address_key(row)
                if key:
                    open_addr_keys.add(key)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return ids, open_addr_keys

    def _confirm_resolutions(self, case_ids: list[str]) -> dict[str, str]:
        """Re-query dropped case_ids to confirm status + capture a resolution note.

        A case that dropped out of the open set is re-fetched by case_id (no
        status filter) to read its current status/closed_date. Returns
        {case_id: note}, e.g. "Closed on 2026-06-14" or, if it's gone entirely,
        "no longer in Austin open cases".
        """
        notes: dict[str, str] = {}
        ids = [c for c in case_ids if c]
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            where = f"case_id in({_quote_list(chunk)})"
            rows = self._fetch_page(where, 0, select="case_id,status,closed_date")
            seen: set[str] = set()
            for row in rows:
                cid = (row.get("case_id") or "").strip()
                if not cid:
                    continue
                seen.add(cid)
                status = (row.get("status") or "").strip()
                closed = (row.get("closed_date") or "")[:10]
                if closed:
                    notes[cid] = f"{status or 'Closed'} on {closed}"
                else:
                    notes[cid] = status or "resolved"
            for cid in chunk:
                notes.setdefault(cid, "no longer in Austin open cases")
        return notes

    def _run_resolution_diff(self, uploaded: list[NoticeData]) -> None:
        """Diff the full open set vs. the prior run and stage resolved records."""
        global LAST_RUN_DIFF, LAST_RUN_RESOLVED, LAST_RUN_REPORT_PATH
        county = "Travis"
        today = datetime.now().strftime("%Y-%m-%d")

        state = ce_state.load_state(county)
        prev_ids = set(state.get("last_run_case_ids") or [])
        prev_records = state.get("last_run_records") or {}

        current_ids, open_addr_keys = self._fetch_all_open_cases()
        diff = ce_state.compute_diff(current_ids, prev_ids, prev_records)

        # Guard: a property can carry several code cases over time. Drop any
        # resolved record whose address STILL has an open case — tagging it
        # "Resolved" would wrongly scope an actively-violating property out of
        # CRM marketing (the cleanup matches by address, not case_id).
        if diff.dropped_records and open_addr_keys:
            before = len(diff.dropped_records)
            diff.dropped_records = [
                r for r in diff.dropped_records
                if _addr_key(r.get("address", ""), r.get("zip", "")) not in open_addr_keys
            ]
            suppressed = before - len(diff.dropped_records)
            if suppressed:
                logger.info(
                    "Code-enforcement resolution: suppressed %d resolved record(s) whose "
                    "address still has an open case", suppressed,
                )

        # Status-confirm the subset we'll actually emit, and attach the note.
        if diff.dropped_records:
            notes = self._confirm_resolutions([r["case_id"] for r in diff.dropped_records])
            for r in diff.dropped_records:
                r["resolution_note"] = notes.get(r["case_id"], "")

        # Accumulate the uploaded-record snapshot (address is our DataSift match
        # key), bounded to the current open set. This carries addresses forward
        # across daily windows so a case uploaded weeks ago can still be
        # rehydrated when it resolves; resolved/closed ids fall out naturally.
        todays_records = ce_state.snapshot_records(uploaded)
        merged = {**prev_records, **todays_records}
        current_records = {cid: rec for cid, rec in merged.items() if cid in current_ids}

        ce_state.commit_run(county, state, current_ids, diff, current_records)
        LAST_RUN_REPORT_PATH = ce_state.write_report_json(county, diff)

        LAST_RUN_DIFF = diff.to_dict()
        LAST_RUN_RESOLVED = [
            ce_state.build_resolved_notice(r, county, today, r.get("resolution_note", ""))
            for r in diff.dropped_records
        ]
        logger.info(
            "Code-enforcement resolution diff (Travis): open=%d, new=%d, resolved=%d, tagged=%d%s",
            len(current_ids), diff.new_count, diff.dropped_count, len(LAST_RUN_RESOLVED),
            f" [guardrail: {diff.guardrail_reason}]" if diff.guardrail_tripped else "",
        )

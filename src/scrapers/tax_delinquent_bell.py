"""Bell County tax-delinquent scraper.

Downloads BellCAD's `BellCAD_Delinquent_Roll_Condensed_*.xlsx` from the data
portal (https://bellcad.org/data-portal/) and emits filtered NoticeData. The
heavy lifting (URL discovery, download, XLSX parsing, owner-name index) lives
in `bell_tax_cache` so the file is shared between this scraper and the
probate/foreclosure address-backfill lookups in `cad_lookup.py`.

Source: Bell Central Appraisal District, Collections Data section.
Updated: ~weekly refresh by BellCAD.
Records: ~12K Real Property delinquencies (after filtering out Personal
  Property / Auto / Mobile Home rows from the ~24K-row source file).

Filters applied to every row:
  1. prop_type == 'Real Property' (skip Personal/Auto/MH).
  2. years_owed → 2+ years delinquent.
  3. Base_Tax_Due → $3,000+ owed.
  4. state_cd not in {A4} (condos), not in {C1, D1} (raw land/agricultural).
  5. Owner name not an HOA (best-effort regex).

Cross-run diff: data/bell_tax_state/, raw archive: data/bell_tax_raw/.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import openpyxl

import bell_tax_cache
from notice_parser import NoticeData, normalize_court_name
from scrapers import register
from scrapers import tax_delinquent_state as state_mod

logger = logging.getLogger(__name__)


# ── Module-level run output (read by main.py for Slack summary) ──────
LAST_RUN_DIFF: dict | None = None
LAST_RUN_STATS: dict | None = None
LAST_RUN_REMOVED: dict | None = None
LAST_RUN_REPORT_PATH: Path | None = None
LAST_RUN_RAW_PATH: Path | None = None
# Sold (dropped-off-roll) NoticeData rehydrated from the prior run's snapshot.
# Collected by main.py after enrichment and appended to the upload CSV tagged
# "Sold" so DataSift applies the tag to the matching record by address.
LAST_RUN_SOLD: list | None = None


# ── Filter thresholds (CLI-overridable) ───────────────────────────────
DEFAULT_MIN_DELINQUENT_YEARS = 2
DEFAULT_MIN_DELINQUENT_AMOUNT = 3000.0


# ── Property-type code labels (matches Travis cleaner conventions) ───
_STATE_CD_LABELS = {
    "A1": "Single Family",
    "A2": "Multi-Family",
    "A4": "Condominium",
    "B1": "Multi-Family (5+)",
    "B2": "Multi-Family (5+)",
    "C1": "Vacant Land",
    "D1": "Agricultural",
    "E1": "Rural",
    "F1": "Commercial",
    "F2": "Industrial",
    "L1": "Commercial Personal",
    "M1": "Mobile Home",
}

# Skip non-targeted property types — match Travis exclusions plus raw-land/ag.
EXCLUDED_STATE_CD = {"A4", "C1", "D1", "E1", "L1", "M1"}

# HOA / management entity owners — same regex spirit as travis_texdel_cleaner.
_HOA_PAT = re.compile(
    r"\b(HOA|HOMEOWNERS|HOMEOWNER'S|OWNERS ASSOC|ASSOCIATION|CONDO ASSN?|"
    r"MANAGEMENT|PROPERTY OWNERS|PROP OWNERS)\b",
    re.IGNORECASE,
)
# Business entity heuristic — surfaces LLC/INC/CORP/TRUST names so we
# preserve them via business_name instead of running them through the
# LAST FIRST flipper.
_BUSINESS_PAT = re.compile(
    r"\b(LLC|L\.L\.C\.|INC|INCORPORATED|CORP|CORPORATION|LTD|LP|"
    r"COMPANY|CO\.?|TRUST|ESTATE|BANK|HOLDINGS|PARTNERS|PARTNERSHIP|"
    r"PROPERTIES|GROUP|FUND|VENTURES|INVESTMENTS)\b",
    re.IGNORECASE,
)


def _is_business(name: str) -> bool:
    return bool(name and _BUSINESS_PAT.search(name))


# Suffixes that Python's .title() mangles — re-uppercase after title-casing.
# Includes legal-entity suffixes (LLC, INC, LP, CO, …), Roman numerals (II–X),
# and professional credentials (MD, DDS, ESQ).
_UPPERCASE_SUFFIX_PAT = re.compile(
    r"\b(LLC|INC|CORP|LP|LLP|PLLC|PA|PC|CO|MD|DDS|DVM|CPA|ESQ|JR|SR|"
    r"II|III|IV|V|VI|VII|VIII|IX|X|"
    r"USA|TX|TXR|HOA|HUD|VA|FBO|ETUX|ETVIR|ETAL)\b\.?",
    re.IGNORECASE,
)


def _title_case(name: str) -> str:
    """Title-case an ALL-CAPS name, then re-uppercase legal/credential suffixes.

    Python's `.title()` lowercases all-caps suffixes (LLC → Llc, IV → Iv).
    We post-process to restore the canonical uppercase form so DataSift CSVs
    look professional ("Joseph Barnes II" not "Joseph Barnes Ii", "Foo LLC"
    not "Foo Llc").
    """
    if not name:
        return ""
    out = name.title() if name.isupper() else name
    # Re-uppercase the matched suffixes
    return _UPPERCASE_SUFFIX_PAT.sub(lambda m: m.group(0).upper(), out)


def _strip_etux_etal(name: str) -> str:
    """Strip trailing ETUX/ETVIR/ETAL clauses + spousal `& <name>` tails.

    Applied to both the individual-name path AND the business-name path
    (e.g., "Lopez Estate Etal" → "Lopez Estate"). The `&` branch is kept
    only for natural names — businesses with `&` in the entity name
    (e.g., "Smith & Jones LLC") preserve the `&` because the regex anchors
    to a tail clause, not a mid-name `&`.
    """
    if not name:
        return ""
    # Drop trailing "<space>(ETUX|ETVIR|ETAL)<word-boundary><anything>"
    # `&` is non-word so handle it as a separate alternation branch.
    return re.sub(
        r"\s+(?:(?:ETUX|ETVIR|ETAL)\b|&\s).*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()


def _parse_years_owed(years_owed_raw: str | int | None) -> tuple[str, int]:
    """Same parsing as bell_tax_cache._parse_years_owed (kept local for clarity).

    Returns (first_year_str, count_of_years_delinquent).
    """
    if years_owed_raw in (None, ""):
        return "", 0
    s = str(years_owed_raw).strip()
    m = re.match(r"^(\d{4})(?:-(\d{4}))?$", s)
    if not m:
        return "", 0
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) else first
    if last < first:
        first, last = last, first
    return str(first), (last - first + 1)


# Care-of prefixes the tax office uses on the FIRST mailing line.
# Detection separates the "live person who handles mail" (heir/executor/
# trustee/family member) from the actual postal address. This person is a
# strong distress-lead signal — surface them as `decision_maker_name` so
# skip-trace + deep-prospecting reports can reach them.
_CARE_OF_PAT = re.compile(
    r"^\s*(?:C\s*/\s*O|CO|ATTN|FBO|%)\s*[:.]?\s*(.+?)\s*$",
    re.IGNORECASE,
)


def _flatten_excel_lines(raw: str) -> list[str]:
    """Split BellCAD's `Ownr_Addr` cell into trimmed non-empty lines.

    BellCAD encodes line breaks as the literal sequence `_x000d_\\n` (XLSX-
    escaped \\r\\n). Excel renders the cell as a multi-line value but it is
    still ONE column — the previous bug joined lines with commas and lost
    the structure. Splitting on the literal newline preserves the per-line
    semantics (line 1 = optional C/O, last line = city/state/zip, middle =
    street + suite/apt).
    """
    if not raw:
        return []
    s = raw.replace("_x000d_", "")
    return [ln.strip() for ln in s.splitlines() if ln.strip()]


def _parse_city_state_zip(line: str) -> tuple[str, str, str]:
    """Parse 'CITY, STATE  ZIP[-PLUS4]' (or unmatched: returns empties)."""
    m = re.match(
        r"^(.+?)\s*,\s*([A-Z]{2})\s+(\d{5})(?:-?\d{4})?\s*$",
        line.strip(),
    )
    if not m:
        return "", "", ""
    return m.group(1).strip(), m.group(2), m.group(3)


def _parse_ownr_addr(raw: str) -> tuple[str, str, str, str, str, str]:
    """Parse Bell's multi-line `Ownr_Addr` cell.

    Returns (street, city, state, zip5, c_o_name, full_clean).
    - street/city/state/zip5: the actual postal mailing.
    - c_o_name: live caretaker if line 1 is a C/O / ATTN / FBO / % prefix.
    - full_clean: single-string version with all lines comma-joined, kept
      for forensics in `mailing_address` (debugging + DataSift `Notes`).

    Layout of the source cell (most common):
      [Optional] line 1: "C/O <name>" / "ATTN <name>" / "%<name>" / "FBO <name>"
      Line 2 (or 1 if no C/O): street + optional apartment/suite
      Last line: "CITY, STATE  ZIP[-PLUS4]"

    Some rows have only 1-2 lines (no city/state/zip line) or a 4-line layout
    (street + suite split). We're permissive: extra middle lines are folded
    into the street, and missing lines just leave fields blank.
    """
    lines = _flatten_excel_lines(raw)
    if not lines:
        return "", "", "", "", "", ""

    full_clean = ", ".join(lines)

    c_o_name = ""
    co_match = _CARE_OF_PAT.match(lines[0])
    # Only treat line 1 as a C/O if there's at least one more line that
    # looks like a real address — otherwise the whole cell is just a C/O
    # caretaker and we keep it as the street (some Bell rows are like that).
    if co_match and len(lines) >= 2:
        c_o_name = co_match.group(1).strip().rstrip(",")
        lines = lines[1:]

    # Try to peel city/state/zip off the last line
    city = state = zip5 = ""
    if len(lines) >= 2:
        c, s, z = _parse_city_state_zip(lines[-1])
        if c and s and z:
            city, state, zip5 = c, s, z
            street_lines = lines[:-1]
        else:
            street_lines = lines
    else:
        street_lines = lines

    # Even if we didn't find a city/state/zip on a separate line, the
    # caller may have a single-line "STREET, CITY ST ZIP" packed all
    # together — try to peel that too.
    if not city and street_lines:
        last = street_lines[-1]
        c, s, z = _parse_city_state_zip(last)
        if c and s and z:
            city, state, zip5 = c, s, z
            street_lines = street_lines[:-1]

    street = " ".join(street_lines).strip()
    return street, (city.title() if city else ""), state, zip5, c_o_name, full_clean


@register("Bell", "tax_delinquent")
class BellTaxDelinquentScraper:
    """Bell County delinquent property roll → NoticeData."""

    COUNTY = "Bell"

    def __init__(
        self,
        min_years: int = DEFAULT_MIN_DELINQUENT_YEARS,
        min_amount: float = DEFAULT_MIN_DELINQUENT_AMOUNT,
        fixture_xlsx: str | Path | None = None,
        skip_target_zip: bool = False,
    ):
        self.min_years = min_years
        self.min_amount = min_amount
        self.fixture_xlsx = Path(fixture_xlsx) if fixture_xlsx else None
        self.skip_target_zip = skip_target_zip

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        global LAST_RUN_DIFF, LAST_RUN_STATS, LAST_RUN_REMOVED
        global LAST_RUN_REPORT_PATH, LAST_RUN_RAW_PATH, LAST_RUN_SOLD
        LAST_RUN_DIFF = None
        LAST_RUN_STATS = None
        LAST_RUN_REMOVED = None
        LAST_RUN_REPORT_PATH = None
        LAST_RUN_RAW_PATH = None
        LAST_RUN_SOLD = None

        logger.info(
            "Bell tax delinquent: filters %d+ years, $%.0f+ owed, target-zip=%s",
            self.min_years, self.min_amount, "OFF" if self.skip_target_zip else "ON",
        )

        # ── Acquire XLSX ──
        if self.fixture_xlsx:
            if not self.fixture_xlsx.exists():
                logger.error("Fixture XLSX not found: %s", self.fixture_xlsx)
                return []
            xlsx_path = self.fixture_xlsx
            raw_bytes = xlsx_path.read_bytes()
        else:
            xlsx_path = bell_tax_cache.iter_delinquent_path()
            if not xlsx_path:
                logger.error("Bell tax delinquent: download failed (no cached file)")
                return []
            raw_bytes = xlsx_path.read_bytes()

        # ── Archive raw bytes (post-mortem audit) ──
        try:
            raw_archive_path, raw_sha = state_mod.archive_raw_bytes(
                self.COUNTY, raw_bytes, suffix=".xlsx",
            )
            LAST_RUN_RAW_PATH = raw_archive_path
            logger.info("Archived raw XLSX: %s", raw_archive_path)
        except Exception as e:
            logger.warning("Raw XLSX archival failed: %s", e)
            raw_archive_path, raw_sha = None, ""

        # ── Parse rows ──
        notices: list[NoticeData] = []
        today = datetime.now().strftime("%Y-%m-%d")
        source_apns: set[str] = set()
        removed = {
            "non_real": 0, "no_year": 0, "under_years": 0, "hoa": 0,
            "prop_type": 0, "under_amt": 0, "zip": 0, "no_owner": 0, "no_apn": 0,
        }

        target_zips: set[str] = set()
        if not self.skip_target_zip:
            try:
                from zip_filter import load_target_zips
                target_zips = set(load_target_zips() or [])
            except Exception:
                pass

        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = list(next(rows))
        except StopIteration:
            wb.close()
            return []
        col = {h: i for i, h in enumerate(header) if h is not None}

        def _g(row, name, default=""):
            i = col.get(name)
            if i is None or i >= len(row):
                return default
            v = row[i]
            return default if v is None else v

        for row in rows:
            if max_notices and len(notices) >= max_notices:
                break

            prop_type = str(_g(row, "prop_type")).strip()
            prop_id = str(_g(row, "prop_id")).strip()

            if prop_type != "Real Property":
                removed["non_real"] += 1
                continue
            if not prop_id:
                removed["no_apn"] += 1
                continue

            # Track all real-property APNs for the diff (even if filtered out
            # by amount/years thresholds — diff guardrail relies on full set).
            source_apns.add(prop_id)

            owner_raw = str(_g(row, "Ownr_Name")).strip()
            if not owner_raw:
                removed["no_owner"] += 1
                continue

            # Property type filter (condos, raw land, ag)
            state_cd = str(_g(row, "state_cd")).strip().upper()
            if state_cd in EXCLUDED_STATE_CD:
                removed["prop_type"] += 1
                continue

            # Years-delinquent filter
            first_year, years_count = _parse_years_owed(_g(row, "years_owed"))
            if years_count == 0:
                removed["no_year"] += 1
                continue
            if self.min_years > 0 and years_count < self.min_years:
                removed["under_years"] += 1
                continue

            # Amount-owed filter
            delq_total_raw = str(_g(row, "Base_Tax_Due")).strip()
            try:
                owed = float(delq_total_raw) if delq_total_raw else 0.0
            except ValueError:
                owed = 0.0
            if self.min_amount > 0 and owed < self.min_amount:
                removed["under_amt"] += 1
                continue

            # Owner classification — business vs person.
            # Both paths get ETUX/ETAL/ETVIR stripped first because the source
            # appends the marker to the END of either kind of name (e.g.
            # "Lopez, Antonio O Estate Etal" or "My Simple Land Llc Etal").
            owner_clean = _strip_etux_etal(owner_raw)
            business_name = ""
            person_name = ""
            if _is_business(owner_clean):
                if _HOA_PAT.search(owner_clean):
                    removed["hoa"] += 1
                    continue
                business_name = _title_case(owner_clean)
            else:
                # Bell uses LAST, FIRST format. Strip the comma so
                # normalize_court_name's flipper handles it consistently.
                no_comma = owner_clean.replace(",", "")
                flipped = normalize_court_name(no_comma)
                person_name = _title_case(flipped) if flipped.isupper() else flipped
                if not person_name:
                    person_name = _title_case(owner_clean)

            # Property address
            situs = str(_g(row, "situs_address")).strip()
            city = str(_g(row, "situs_city")).strip().title()
            zip_raw = str(_g(row, "situs_zip")).strip()
            zip5 = zip_raw[:5] if zip_raw else ""

            if target_zips and zip5 and zip5 not in target_zips:
                removed["zip"] += 1
                continue

            # Mailing address — multi-line parse separates the C/O caretaker
            # (if any) from the actual postal street. C/O entries are a
            # strong distress signal: the named person is who actually gets
            # the property's mail (heir, executor, trustee, family member).
            mail_street, mail_city, mail_state, mail_zip, c_o_name, mail_full = (
                _parse_ownr_addr(str(_g(row, "Ownr_Addr")))
            )

            owner_name_out = business_name or person_name

            notice = NoticeData(
                notice_type="tax_delinquent",
                county="Bell",
                state="TX",
                date_added=today,
                address=situs.title() if situs else "",
                city=city,
                zip=zip5,
                owner_name=owner_name_out,
                parcel_id=prop_id,
                source_url=f"https://esearch.bellcad.org/Property/View/{prop_id}",
            )
            notice.business_name = business_name
            notice.tax_owner_name = owner_raw
            notice.mailing_address = mail_full
            # Surface the C/O caretaker as a decision-maker candidate so
            # downstream skip-trace + DataSift see the live person who
            # actually handles the property's mail.
            if c_o_name:
                # Strip ETUX/ETAL/spousal-tails from the C/O person too —
                # often the caretaker entry is "GILPIN, JAMES E ETUX AMY M"
                # where Amy is a joint mailing-list addition, not a separate
                # decision-maker. Also drop the comma so the name title-cases
                # properly ("GILPIN, JAMES E" → "James E Gilpin").
                co_clean = _strip_etux_etal(c_o_name)
                notice.decision_maker_name = _title_case(co_clean.replace(",", "").strip())
                notice.decision_maker_source = "tax_record_c_o"
                notice.decision_maker_status = "tax_caretaker"
                notice.dm_relationship = "care_of_caretaker"
            if mail_full:
                notice.owner_street = mail_street
                notice.owner_city = mail_city
                notice.owner_state = mail_state or "TX"
                notice.owner_zip = mail_zip
            notice.tax_delinquent_amount = delq_total_raw
            notice.tax_delinquent_years = str(years_count)
            if state_cd in _STATE_CD_LABELS:
                notice.property_type = _STATE_CD_LABELS[state_cd]
            elif state_cd:
                notice.property_type = state_cd
            co_line = f"\nC/O: {c_o_name}" if c_o_name else ""
            notice.raw_text = (
                f"prop_id: {prop_id}\n"
                f"Owner: {owner_raw}\n"
                f"Address: {situs}, {city}, TX {zip5}\n"
                f"Mailing: {mail_full}{co_line}\n"
                f"Delinquent: ${delq_total_raw} (since {first_year}, {years_count} year{'s' if years_count != 1 else ''})\n"
                f"Legal: {_g(row, 'legal_desc')}"
            )
            notices.append(notice)

        wb.close()

        # ── Cross-run diff + state commit ──
        try:
            state = state_mod.load_state(self.COUNTY)
            prev_apns = set(state.get("last_run_apns") or [])
            prev_records = state.get("last_run_records") or {}
            current_records = state_mod.snapshot_records(notices)
            diff = state_mod.compute_diff(source_apns, prev_apns, prev_records)
            stats = state_mod.build_stats(notices)

            if raw_archive_path is not None:
                state_mod.commit_run(
                    self.COUNTY, state,
                    current_apns=source_apns,
                    raw_path=raw_archive_path,
                    raw_sha=raw_sha,
                    diff=diff,
                    current_records=current_records,
                )
            report_path = state_mod.write_report_json(
                self.COUNTY, diff, stats, removed,
                raw_path=raw_archive_path or "",
            )
            # Rehydrate dropped-off-roll parcels into Sold-tagged records.
            LAST_RUN_SOLD = [
                state_mod.build_sold_notice(rec, self.COUNTY, today)
                for rec in diff.dropped_records
            ]
            LAST_RUN_DIFF = diff.to_dict()
            LAST_RUN_STATS = stats
            LAST_RUN_REMOVED = removed
            LAST_RUN_REPORT_PATH = report_path
            if diff.guardrail_tripped:
                logger.warning(
                    "Bell tax-delinquent guardrail tripped: %s — prior state preserved",
                    diff.guardrail_reason,
                )
            elif diff.is_first_run:
                logger.info(
                    "Bell tax-delinquent first run: seeding state with %d APNs",
                    len(source_apns),
                )
            else:
                logger.info(
                    "Bell tax-delinquent cross-run diff: NEW=%d REPEAT=%d DROPPED=%d "
                    "(%d in our upload → tagged Sold — see %s)",
                    diff.new_count, diff.repeat_count, diff.dropped_count,
                    len(diff.dropped_records), report_path,
                )
        except Exception:
            logger.exception("Bell tax-delinquent diff/state failed — continuing")

        logger.info(
            "Bell tax delinquent: %d records kept (filtered: %s)",
            len(notices),
            ", ".join(f"{k}={v}" for k, v in removed.items() if v) or "none",
        )
        return notices

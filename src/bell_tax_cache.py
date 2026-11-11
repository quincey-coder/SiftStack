"""Bell County tax data cache — owner name → property + delinquency lookup.

Mirrors `travis_tax_cache` for Travis County. BellCAD has no live API, so the
master cross-reference comes from the Appraisal Data Condensed XLSX (~190K
rows, full county). The Delinquent Roll Condensed XLSX (~24K rows) overlays
authoritative tax-delinquency fields onto matching properties.

Both XLSX files have rotating date stamps in their filenames; this module
discovers the latest URL by fetching the BellCAD data portal index on each
download.

Indices (lazy, process-wide):
  _INDEX[normalized_last_name] → list[property dict]
  _ADDR_INDEX[(normalized_street, zip5)] → property dict

Each property dict carries `source` = "delinquent_situs" (authoritative) or
"appraisal_master" (heuristic). Delinquent records win on address lookup.
Mirrors the pattern in `travis_tax_cache._load_delq` / `_load_cur`.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
import json
import time
from pathlib import Path
from typing import Iterable

import requests

import config

logger = logging.getLogger(__name__)


_INDEX: dict[str, list[dict]] | None = None
_ADDR_INDEX: dict[tuple[str, str], dict] | None = None
# Parcel index: {prop_id: record | "__AMBIG__"}. Bell's prop_id (quickrefid) is an
# exact per-parcel key, so this is a clean exact-match owner lookup (like Travis).
_PARCEL_INDEX: dict[str, dict | str] | None = None


# ── URL discovery ────────────────────────────────────────────────────
def _fetch_portal_html() -> str:
    r = requests.get(config.BELLCAD_DATA_PORTAL_URL, timeout=30)
    r.raise_for_status()
    return r.text


# pattern -> URL discovered this process; lets delinquent_meta() answer without
# a second portal GET when the sidecar is missing (pre-sidecar cache files).
_DISCOVERED: dict[str, str] = {}


def discover_latest_url(pattern: str) -> str | None:
    """Find the latest matching XLSX URL on the BellCAD data portal page.

    The portal lists multiple historical files for some categories (e.g.,
    2024 + 2025 appraisal exports). When multiple match, prefer the one
    with the highest YYYYMMDD date stamp at the end of the filename.
    """
    try:
        html = _fetch_portal_html()
    except Exception as e:
        logger.warning("BellCAD portal fetch failed: %s", e)
        return None

    # Anchor URLs in the page have absolute hrefs
    candidates = re.findall(
        r'https://[^\s"\'<>]+?' + pattern,
        html,
    )
    if not candidates:
        # Pattern may bleed past the URL terminator; broaden and re-filter
        candidates = re.findall(r'https://[^\s"\'<>]+\.xlsx', html)
        candidates = [c for c in candidates if re.search(pattern, c)]
    if not candidates:
        logger.warning("BellCAD portal had no match for %s", pattern)
        return None

    candidates.sort(key=_url_sort_key, reverse=True)
    _DISCOVERED[pattern] = candidates[0]
    return candidates[0]


_FILENAME_DATE_RE = re.compile(r"(\d{8})\.(?:xlsx|rar|zip)$", re.I)


def filename_datestamp(name: str) -> str:
    """Return the YYYYMMDD export stamp at the end of a portal filename ("" if none)."""
    m = _FILENAME_DATE_RE.search((name or "").rsplit("/", 1)[-1])
    return m.group(1) if m else ""


def _url_sort_key(url: str) -> tuple[str, int]:
    """Newest export date first; on a tie the highest roll year.

    2026-09-22 posted `...Condensed-2026-20260922.xlsx` AND
    `...Condensed-2027-20260922.xlsx` (next-year working roll). The 2027 file
    carries the latest ownership, which is what the cross-reference needs.
    """
    base = url.rsplit("/", 1)[-1]
    date = filename_datestamp(base)
    rest = base.replace(date, "") if date else base
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", rest)]
    return (date, max(years) if years else 0)


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def _write_meta(path: Path, url: str) -> None:
    """Sidecar next to a cached file: where it came from and the export stamp."""
    meta = {
        "url": url,
        "datestamp": filename_datestamp(url),
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        _meta_path(path).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover - best effort
        logger.debug("meta sidecar write failed for %s: %s", path, e)


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(_meta_path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def delinquent_meta(refresh_if_missing: bool = True) -> dict:
    """{url, datestamp, downloaded_at} for the cached delinquent roll.

    A cache file written before the sidecar existed has no meta; with
    `refresh_if_missing` the portal index is fetched once to recover the
    current URL/date (one small GET, no download).
    """
    path = _cache_dir() / "BellCAD_Delinquent_Roll_Condensed_latest.xlsx"
    meta = _read_meta(path)
    if not meta.get("datestamp"):
        pattern = config.BELLCAD_DELINQUENT_ROLL_PATTERN
        url = _DISCOVERED.get(pattern)
        if not url and refresh_if_missing:
            url = discover_latest_url(pattern)
        if url:
            meta = {"url": url, "datestamp": filename_datestamp(url), "downloaded_at": ""}
    return meta


def roll_age_days(datestamp: str, today: date | None = None) -> int | None:
    """Days between a YYYYMMDD export stamp and today (None if unparseable)."""
    if not datestamp or len(datestamp) != 8 or not datestamp.isdigit():
        return None
    try:
        d = date(int(datestamp[:4]), int(datestamp[4:6]), int(datestamp[6:8]))
    except ValueError:
        return None
    return ((today or date.today()) - d).days


# ── Cache I/O ────────────────────────────────────────────────────────
def _cache_dir() -> Path:
    base = Path(config.BELL_TAX_CACHE_DIR)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _is_fresh(path: Path, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl_hours * 3600


def _download(url: str, dest: Path) -> int:
    logger.info("Downloading %s → %s", url, dest)
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    total = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
            total += len(chunk)
    os.replace(tmp, dest)
    logger.info("  Saved %.1f MB to %s", total / (1 << 20), dest)
    return total


def download_if_stale(force: bool = False) -> tuple[Path | None, Path | None]:
    """Download both Bell XLSX files if missing or stale.

    Returns (delinquent_path, appraisal_path). Either may be None if the
    portal didn't surface a matching link (network error or layout change).
    """
    ttl = config.BELL_TAX_CACHE_TTL_HOURS
    cache = _cache_dir()
    delq_path = cache / "BellCAD_Delinquent_Roll_Condensed_latest.xlsx"
    appr_path = cache / "BellCAD_Appraisal_Data_Condensed_latest.xlsx"

    delq_url = appr_url = None
    if force or not _is_fresh(delq_path, ttl):
        delq_url = discover_latest_url(config.BELLCAD_DELINQUENT_ROLL_PATTERN)
    if force or not _is_fresh(appr_path, ttl):
        appr_url = discover_latest_url(config.BELLCAD_APPRAISAL_ROLL_PATTERN)

    if delq_url:
        try:
            _download(delq_url, delq_path)
            _write_meta(delq_path, delq_url)
        except Exception as e:
            logger.warning("Bell delinquent download failed: %s", e)
            if not delq_path.exists():
                delq_path = None
    if appr_url:
        try:
            _download(appr_url, appr_path)
            _write_meta(appr_path, appr_url)
        except Exception as e:
            logger.warning("Bell appraisal download failed: %s", e)
            if not appr_path.exists():
                appr_path = None

    return (
        delq_path if delq_path and delq_path.exists() else None,
        appr_path if appr_path and appr_path.exists() else None,
    )


# ── Index helpers ────────────────────────────────────────────────────
def _normalize_street(street: str) -> str:
    if not street:
        return ""
    s = street.upper()
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_last(name: str) -> str:
    """Pull the last-name token used as the index key.

    Bell delinquent owner name is `LAST, FIRST [ETUX/ETAL ...]` — so the
    pre-comma chunk is the surname. Fall back to the first whitespace token
    when there's no comma (matches the Travis behavior when format drifts).
    """
    if not name:
        return ""
    s = name.strip().upper()
    if "," in s:
        return s.split(",", 1)[0].strip()
    for suf in (" JR", " SR", " II", " III", " IV", " MD", " DDS", " ESQ"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    parts = s.split()
    return parts[0] if parts else ""


def _clean_mailing(addr: str) -> str:
    """Bell's `Ownr_Addr` packs street + city/state/zip into one field with
    `_x000d_\\n` literal newline markers. Collapse to a single line."""
    if not addr:
        return ""
    s = addr.replace("_x000d_", "").replace("\r", " ").replace("\n", ", ")
    s = re.sub(r"\s+", " ", s).strip().strip(",").strip()
    return s


# ── Owner mailing split (2026-11-11) ─────────────────────────────────
# BellCAD's `Ownr_Addr` is one cell holding the whole mailing block. The
# delinquent roll separates the lines with `_x000d_\n`; the 2026-09 appraisal
# export DROPPED the separators, so the street and city arrive glued:
#   '165 CEDAR RIDGE CIRKILLEEN, TX 76542'
#   'C/O DOLORES LARA806 N 8TH STTEMPLE, TX 76501-2642'
# Every Bell mailing therefore reached DataSift as one street string, and the
# mailing CITY fell back to the PROPERTY city (an owner in Troy was mailed in
# Temple). split_owner_address() returns (street, city, state, zip5); glued
# cities are recognised against a vocabulary learned from the separated rows
# of the same files (see load_index) plus BASE_CITY_VOCAB.
BASE_CITY_VOCAB = frozenset({
    "KILLEEN", "TEMPLE", "BELTON", "HARKER HEIGHTS", "NOLANVILLE", "SALADO", "TROY",
    "MOODY", "HOLLAND", "ROGERS", "BARTLETT", "LITTLE RIVER", "LITTLE RIVER ACADEMY",
    "COPPERAS COVE", "FORT HOOD", "FORT CAVAZOS", "MORGANS POINT RESORT", "ACADEMY",
    "PENDLETON", "HEIDENHEIMER", "KEMPNER", "LAMPASAS", "GATESVILLE", "WACO",
    "AUSTIN", "ROUND ROCK", "GEORGETOWN", "SAN ANTONIO", "HOUSTON", "DALLAS",
    "FORT WORTH", "EL PASO", "KYLE", "ALLEN", "LANCASTER", "CEDAR PARK",
    "PFLUGERVILLE", "LEANDER", "HUTTO", "TAYLOR", "JARRELL", "FLORENCE",
})
_MAIL_TAIL_RE = re.compile(r"^(?P<head>.*?),?\s+(?P<st>[A-Z]{2})\s+(?P<zip>\d{5})(?:-?\d{4})?\s*$")
_CARE_OF_RE = re.compile(r"^(?:C/O|ATTN:?|IN CARE OF)\b", re.I)
_GLUE_SUFFIXES = ("DR|RD|ST|LN|CT|CIR|AVE|BLVD|WAY|LOOP|LP|PL|TRL|PKWY|HWY|CV|TER|PATH|RUN|"
                  "PASS|BND|XING|SQ|PT|HOLW|TRCE|CRK|RDG|VW|PIKE|ROW|ALY")


def _mail_lines(raw: str) -> list[str]:
    s = (raw or "").replace("_x000d_", "\n").replace("\r", "\n")
    return [re.sub(r"\s+", " ", ln).strip().strip(",").strip() for ln in s.split("\n") if ln.strip()]


def city_from_separated(raw: str) -> str:
    """The city of a mailing block that still HAS line separators ('' if none)."""
    lines = _mail_lines(raw)
    if len(lines) < 2:
        return ""
    m = _MAIL_TAIL_RE.match(lines[-1].upper())
    if not m:
        return ""
    head = m.group("head").strip().rstrip(",").strip()
    return head if head and not re.search(r"\d", head) else ""


def _pick_street_line(lines: list[str]) -> str:
    """From the street lines of a block, drop care-of/attention lines when a
    real street line (house number or PO Box) is present."""
    real = [ln for ln in lines if not _CARE_OF_RE.match(ln)]
    real = real or lines
    numbered = [ln for ln in real if re.search(r"\d", ln) or "BOX" in ln.upper()]
    return (numbered[-1] if numbered else real[-1]) if real else ""


def _strip_glued_prefix(street: str) -> str:
    """'C/O DOLORES LARA806 N 8TH ST' / 'AGUSTINA1225 LIBERTY BELL LP' -> from the
    first house number on. A name glued in front of the number is never part of
    the street. PO Boxes are left alone."""
    if re.match(r"^\s*(?:P\.?\s*O\.?\s*)?BOX\b", street, re.I) or re.match(r"^\s*\d", street):
        return street
    m = re.search(r"(?<![A-Z0-9 ])\d|(?<=[A-Za-z])\d|\s\d", street)
    if m and re.search(r"[A-Za-z]", street[: m.start()]):
        if re.search(r"\bBOX\s*$", street[: m.start()], re.I):
            return street
        return street[m.start():].strip()
    return street


def split_owner_address(raw: str, city_vocab: Iterable[str] = ()) -> tuple[str, str, str, str]:
    """Split a BellCAD ``Ownr_Addr`` cell into (street, city, state, zip5).

    Never guesses a city it cannot find: an unsplittable glued block returns the
    street part with a blank city (the formatter then leaves Mailing City blank
    rather than copying the property city)."""
    lines = _mail_lines(raw)
    if not lines:
        return "", "", "", ""
    last = lines[-1].upper()
    m = _MAIL_TAIL_RE.match(last)
    if not m:
        return _strip_glued_prefix(_pick_street_line(lines)), "", "", ""
    state, zip5 = m.group("st"), m.group("zip")
    head = m.group("head").strip().rstrip(",").strip()
    if len(lines) >= 2 and head and not re.search(r"\d", head):
        # Separated block: last line = "CITY, ST ZIP", the street is above it.
        return _strip_glued_prefix(_pick_street_line(lines[:-1])), head, state, zip5
    # Glued block: '<street><CITY>' (or '<street> <CITY>') in one line.
    glued = head
    vocab = sorted({c.upper() for c in (set(city_vocab) | BASE_CITY_VOCAB) if c}, key=len, reverse=True)
    street, city = "", ""
    for c in vocab:
        if glued.endswith(c) and len(glued) > len(c):
            cut = len(glued) - len(c)
            # The street must still end in a word/number — reject splits that
            # leave nothing street-like ('TEMPLE' alone).
            if re.search(r"[A-Z0-9]\s*$", glued[:cut]) and re.search(r"\d|BOX", glued[:cut]):
                street, city = glued[:cut].strip(), c
                break
    if not city:
        # A street suffix glued to an unknown (often out-of-state) city:
        # '11720 HENLEY CTWALDORF', '5405 CLAIRMONT LNN CHARLESTON'.
        m3 = re.match(r"^(\d.*\b(?:" + _GLUE_SUFFIXES + r"))([A-Z][A-Z .'-]{2,})$", glued)
        if m3:
            street, city = m3.group(1).strip(), m3.group(2).strip()
    if not city:
        # A house number / highway number glued to the city: '2268SALADO'.
        m2 = re.match(r"^(.*\d)([A-Z][A-Z .'-]{2,})$", glued)
        if m2:
            street, city = m2.group(1).strip(), m2.group(2).strip()
        else:
            street = glued
    if len(lines) >= 2:
        street = _pick_street_line(lines[:-1] + [street])
    return _strip_glued_prefix(street), city, state, zip5


def _apply_mailing_split(records: Iterable[dict], city_vocab: set[str]) -> int:
    """Rewrite each record's packed ``mailing`` into mailing/mcity/mstate/mzip."""
    n = 0
    for rec in records:
        raw = rec.pop("_mailing_raw", None)
        if raw is None:
            continue
        street, city, state, zip5 = split_owner_address(raw, city_vocab)
        rec["mailing"] = street
        rec["mcity"] = city
        rec["mstate"] = state
        rec["mzip"] = zip5
        n += 1
    return n


def _split_situs_addr(addr: str) -> tuple[str, str, str]:
    """Bell appraisal `Situs_Addr` packs street + city/state/zip on one line:
        ' FM 2410, HARKER HEIGHTS TX  76548'
    Returns (street, city, zip5).
    """
    if not addr:
        return "", "", ""
    s = re.sub(r"\s+", " ", addr).strip().strip(",")
    # Strategy: split on the last comma first (street vs city/state/zip)
    if "," in s:
        street, tail = s.rsplit(",", 1)
    else:
        # Some rows don't use a comma — try to split before "TX" suffix
        m = re.match(r"^(.*?)\s+([A-Z][A-Z ]+?\s+TX\s+\d{5}.*)$", s)
        if m:
            street, tail = m.group(1), m.group(2)
        else:
            return s, "", ""
    street = street.strip()
    tail = tail.strip()
    m2 = re.match(r"^(.*?)\s+TX\s+(\d{5})", tail)
    if m2:
        return street, m2.group(1).strip().title(), m2.group(2)
    return street, tail.title(), ""


# ── Loaders ──────────────────────────────────────────────────────────
def _parse_years_owed(years_owed_raw: str | int | None) -> tuple[str, int]:
    """Bell's `years_owed` is either a single year ('2025') or a range
    ('2014-2025'). Returns (first_year_str, count_of_years).
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


def _load_delinquent(
    path: Path,
    idx: dict[str, list[dict]],
    addr_idx: dict[tuple[str, str], dict],
) -> int:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = list(next(rows))
    except StopIteration:
        wb.close()
        return 0
    col = {h: i for i, h in enumerate(header) if h is not None}
    count = 0
    for row in rows:
        prop_type = (row[col.get("prop_type", -1)] if col.get("prop_type") is not None else "") or ""
        if str(prop_type).strip() != "Real Property":
            continue
        owner = (row[col["Ownr_Name"]] if "Ownr_Name" in col else "") or ""
        owner = str(owner).strip()
        if not owner:
            continue
        key = _normalize_last(owner)
        if not key:
            continue
        situs = (row[col["situs_address"]] if "situs_address" in col else "") or ""
        situs = str(situs).strip()
        if not situs:
            continue
        city = str(row[col.get("situs_city", -1)] or "").strip().title() if "situs_city" in col else ""
        zip5 = str(row[col.get("situs_zip", -1)] or "").strip()[:5] if "situs_zip" in col else ""
        years_owed_raw = row[col.get("years_owed", -1)] if "years_owed" in col else ""
        first_year, years_count = _parse_years_owed(years_owed_raw)
        delq_total = str(row[col.get("Base_Tax_Due", -1)] or "").strip() if "Base_Tax_Due" in col else ""
        prop_id = str(row[col["prop_id"]] if "prop_id" in col else "").strip()
        state_cd = str(row[col.get("state_cd", -1)] or "").strip() if "state_cd" in col else ""
        legal = str(row[col.get("legal_desc", -1)] or "").strip() if "legal_desc" in col else ""
        ownr_raw = str(row[col.get("Ownr_Addr", -1)] or "") if "Ownr_Addr" in col else ""
        ownr_addr = _clean_mailing(ownr_raw)
        record = {
            "fullname": owner,
            "situsaddress": situs,
            "scity": city.upper() if city else "",
            "szip": zip5,
            "quickrefid": prop_id,
            "totalpropmktvalue": "",
            "propertytypedesc": state_cd,
            "delinquent_total": delq_total,
            "years_delinquent": str(years_count) if years_count else "",
            "first_year_delinquent": first_year,
            "mailing": ownr_addr,
            "_mailing_raw": ownr_raw,
            "legal_desc": legal,
            "source": "delinquent_situs",
        }
        idx.setdefault(key, []).append(record)
        addr_idx[(_normalize_street(situs), zip5)] = record
        count += 1
    wb.close()
    return count


def _load_appraisal(
    path: Path,
    idx: dict[str, list[dict]],
    addr_idx: dict[tuple[str, str], dict],
) -> int:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = list(next(rows))
    except StopIteration:
        wb.close()
        return 0
    col = {h: i for i, h in enumerate(header) if h is not None}
    count = 0
    for row in rows:
        prop_type_cd = str(row[col.get("prop_type_cd", -1)] or "").strip() if "prop_type_cd" in col else ""
        if prop_type_cd != "R":
            continue  # skip personal property, autos, mobile homes
        # BellCAD's export header for the owner column has drifted between
        # "Owner_name" (current) and "Ownr_Name" (older). Match either so a
        # header rename can't silently zero out the whole appraisal index.
        owner_col = next(
            (c for c in ("Owner_name", "Ownr_Name", "owner_name", "Owner_Name", "OwnerName")
             if c in col),
            None,
        )
        owner = str(row[col[owner_col]] if owner_col else "").strip()
        if not owner:
            continue
        key = _normalize_last(owner)
        if not key:
            continue
        situs_raw = str(row[col["Situs_Addr"]] if "Situs_Addr" in col else "").strip()
        if not situs_raw:
            continue
        street, city, zip5 = _split_situs_addr(situs_raw)
        if not street:
            continue
        prop_id = str(row[col["prop_id"]] if "prop_id" in col else "").strip()
        market = str(row[col.get("market", -1)] or "").strip() if "market" in col else ""
        state_cd = str(row[col.get("state_cd", -1)] or "").strip() if "state_cd" in col else ""
        legal = str(row[col.get("legal_desc", -1)] or "").strip() if "legal_desc" in col else ""
        ownr_raw = str(row[col.get("Ownr_Addr", -1)] or "") if "Ownr_Addr" in col else ""
        ownr_addr = _clean_mailing(ownr_raw)
        record = {
            "fullname": owner,
            "situsaddress": street,
            "scity": city.upper() if city else "",
            "szip": zip5,
            "quickrefid": prop_id,
            "totalpropmktvalue": market,
            "propertytypedesc": state_cd,
            "delinquent_total": "",
            "years_delinquent": "",
            "mailing": ownr_addr,
            "_mailing_raw": ownr_raw,
            "legal_desc": legal,
            "source": "appraisal_master",
        }
        idx.setdefault(key, []).append(record)
        # Only fill address index when no delinquent record already owns this key
        addr_idx.setdefault((_normalize_street(street), zip5), record)
        count += 1
    wb.close()
    return count


def load_index(force_download: bool = False) -> dict[str, list[dict]]:
    global _INDEX, _ADDR_INDEX
    if _INDEX is not None and not force_download:
        return _INDEX

    delq_path, appr_path = download_if_stale(force=force_download)
    idx: dict[str, list[dict]] = {}
    addr_idx: dict[tuple[str, str], dict] = {}

    if delq_path:
        logger.info("Loading Bell delinquent XLSX into index…")
        n = _load_delinquent(delq_path, idx, addr_idx)
        logger.info("  %d delinquent-situs records indexed", n)
    else:
        logger.warning("Bell delinquent file unavailable — name lookups limited to appraisal master")

    if appr_path:
        logger.info("Loading Bell appraisal master XLSX into index (large file, ~30s)…")
        n = _load_appraisal(appr_path, idx, addr_idx)
        logger.info("  %d appraisal-master records indexed", n)
    else:
        logger.warning("Bell appraisal master unavailable — probate/foreclosure backfill disabled for Bell")

    # Split every packed owner mailing into street/city/state/zip. The city
    # vocabulary comes from the blocks that still carry line separators (the
    # delinquent roll) plus every situs city, so glued appraisal rows resolve.
    vocab: set[str] = set()
    for lst in idx.values():
        for rec in lst:
            c = city_from_separated(rec.get("_mailing_raw") or "")
            if c:
                vocab.add(c)
            if rec.get("scity"):
                vocab.add(rec["scity"].upper())
    n_split = _apply_mailing_split((r for lst in idx.values() for r in lst), vocab)
    logger.info("  owner mailing split: %d records (%d known cities)", n_split, len(vocab))

    # Build the parcel index from the loaded records (delinquent-situs is
    # authoritative; two different owners at one prop_id → ambiguous, declined).
    global _PARCEL_INDEX
    parcel_idx: dict[str, dict | str] = {}
    for lst in idx.values():
        for rec in lst:
            q = (rec.get("quickrefid", "") or "").strip()
            if not q:
                continue
            existing = parcel_idx.get(q)
            if existing is None:
                parcel_idx[q] = rec
            elif existing == "__AMBIG__":
                if rec.get("source") == "delinquent_situs":
                    parcel_idx[q] = rec
            elif existing.get("source") == "delinquent_situs":
                continue
            elif rec.get("source") == "delinquent_situs":
                parcel_idx[q] = rec
            elif _normalize_last(existing.get("fullname", "")) != _normalize_last(
                rec.get("fullname", "")
            ):
                parcel_idx[q] = "__AMBIG__"

    _INDEX = idx
    _ADDR_INDEX = addr_idx
    _PARCEL_INDEX = parcel_idx
    logger.info(
        "Bell tax cache ready: %d name keys, %d address keys, %d parcel keys, "
        "%d total records",
        len(idx), len(addr_idx), len(parcel_idx), sum(len(v) for v in idx.values()),
    )
    return idx


def search_by_parcel(parcel_id: str) -> dict | None:
    """Return the Bell property record for an exact prop_id match, or None
    (unknown, or ambiguous across owners). Mirrors travis_tax_cache."""
    global _PARCEL_INDEX
    if _PARCEL_INDEX is None:
        load_index()
    if _PARCEL_INDEX is None:
        return None
    key = (parcel_id or "").strip()
    if not key:
        return None
    hit = _PARCEL_INDEX.get(key)
    return hit if isinstance(hit, dict) else None


def search_by_name(last_name: str, first_name: str = "") -> list[dict]:
    """Return candidate property dicts matching `last_name` (exact key match).

    Matches the WCAD-shaped result dicts so `cad_lookup.lookup_property_by_name`
    can score them with the same logic.
    """
    idx = load_index()
    key = (last_name or "").strip().upper()
    if not key:
        return []
    return idx.get(key, [])


def search_by_address(street: str, zip5: str) -> dict | None:
    global _ADDR_INDEX
    if _ADDR_INDEX is None:
        load_index()
    if _ADDR_INDEX is None:
        return None
    zip5 = (zip5 or "").strip()[:5]
    if not street or not zip5:
        return None
    return _ADDR_INDEX.get((_normalize_street(street), zip5))


def iter_delinquent_path() -> Path | None:
    """Return the cached delinquent XLSX path (downloading if stale).

    Used by `tax_delinquent_bell.py` for the primary scrape — keeps the
    download in one place so the cache is shared with master-roll lookups.
    """
    delq_path, _ = download_if_stale(force=False)
    return delq_path

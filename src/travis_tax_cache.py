"""Travis Tax Office bulk CSV cache — owner name → property address lookup.

Downloads a daily snapshot of Travis County property/owner data from two
public Tax Office endpoints and builds an in-memory owner-name index used
as Tier 1 of the probate decedent → property address lookup.

Two CSVs feed the index:

  TaxDelqOpenData.csv (~3 MB, ~13K rows):
    Owner Name + Street Number + Street Name + Property Zip + Parcel ID +
    Mailing Address. Has authoritative SITUS (property physical location).
    Only covers tax-delinquent properties.

  TaxCurOpenData.csv (~266 MB, ~500K rows):
    Owner Name (NAMELF) + Mailing Address + Parcel ID. NO situs column.
    Covers every property with tax activity. For probate decedents
    (individuals, not LLCs) the mailing address is almost always their
    home property, so we treat it as situs when delinquent-CSV misses.

Both files are downloaded via a 24h TTL cache. The index maps
normalized_last_name → list of property candidates, each tagged with
source="delinquent_situs" (authoritative) or "current_mailing" (heuristic).
Callers in cad_lookup.py do fuzzy name scoring and prefer delinquent_situs.
"""

import csv
import logging
import os
import time
from pathlib import Path

import requests

import config
import travis_tax_schema as schema

logger = logging.getLogger(__name__)

# Lazy-loaded, process-wide index: {normalized_last_name: [property dicts]}
_INDEX: dict[str, list[dict]] | None = None

# Lazy-loaded address index: {(normalized_street, zip5): property dict}
# Built from both delinquent (authoritative situs) and current CSVs.
# Delinquent entries overwrite current-mailing entries at the same key
# because they carry real tax data.
_ADDR_INDEX: dict[tuple[str, str], dict] | None = None

# Lazy-loaded parcel index: {parcel10: property dict | "__AMBIG__"}.
# Keyed by the 10-digit TCAD geo-id (first 10 digits of the 14-digit PARCEL /
# Account #), which is exactly the format Austin's code-enforcement feed emits
# as `parcelid`. This is an EXACT-match owner lookup that sidesteps the
# street-normalization fuzz that caps the address path — the high-leverage path
# for code-violation / absentee-owner records. A prefix shared by two different
# current-roll owners (condos/apartments with non-0000 property suffixes) is
# marked "__AMBIG__" and not returned; a delinquent-situs record is authoritative.
_PARCEL_INDEX: dict[str, dict | str] | None = None

# Lazy sibling of _ADDR_INDEX for the street-only fallback in
# search_by_address: {normalized_street: [zip5, ...]} — Austin-area ZIPs only.
_STREET_FALLBACK: dict[str, list[str]] | None = None

_FIELD_LIMIT = 10 * 1024 * 1024  # 10 MB — TaxCurOpenData has long quoted rows


def _normalize_parcel(parcel: str) -> str:
    """Reduce a TCAD/Austin parcel identifier to its 10-digit geo-id key.

    TCAD `PARCEL` / `Account #` are 14 digits (10-digit geo-id + 4-digit
    property suffix, usually '0000'); Austin `parcelid` is the bare 10-digit
    geo-id. Stripping to the first 10 digits makes them align."""
    import re as _re
    digits = _re.sub(r"\D", "", parcel or "")
    return digits[:10] if len(digits) >= 10 else ""


def _add_parcel(parcel_idx: dict, record: dict) -> None:
    """Insert a record into the parcel index under its 10-digit geo key.

    Delinquent-situs records are authoritative (kept over current-mailing).
    Two *different* current-mailing owners at the same geo key → '__AMBIG__'
    (a condo/apartment complex), which `search_by_parcel` declines to answer."""
    pkey = _normalize_parcel(record.get("quickrefid", ""))
    if not pkey:
        return
    existing = parcel_idx.get(pkey)
    is_delq = record.get("source") == "delinquent_situs"
    if existing is None:
        parcel_idx[pkey] = record
    elif existing == "__AMBIG__":
        if is_delq:
            parcel_idx[pkey] = record  # authoritative resolves ambiguity
    else:  # existing is a dict
        if existing.get("source") == "delinquent_situs":
            return  # authoritative already present — keep it
        if is_delq:
            parcel_idx[pkey] = record  # authoritative wins over current_mailing
        elif _normalize_last(existing.get("fullname", "")) != _normalize_last(
            record.get("fullname", "")
        ):
            # Two genuinely different owners at one geo → condo/apartment.
            # Compare on surname so mere formatting variants of the SAME owner
            # ("SMITH JOHN & MARY" vs "SMITH JOHN") don't trip the guard.
            parcel_idx[pkey] = "__AMBIG__"


# Spelled-out street suffix/directional words → the USPS abbreviations TCAD
# uses. Applied token-wise and SYMMETRICALLY (index build + lookup), so keys
# only ever collapse toward each other — external feeds that spell words out
# ("2105 East Cesar Chavez Street", CTECC fire feed) match TCAD's
# "E CESAR CHAVEZ ST" style keys.
_STREET_TOKEN_MAP = {
    "STREET": "ST", "DRIVE": "DR", "AVENUE": "AVE", "BOULEVARD": "BLVD",
    "ROAD": "RD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
    "PLACE": "PL", "TERRACE": "TER", "TRAIL": "TRL", "PARKWAY": "PKWY",
    "HIGHWAY": "HWY", "COVE": "CV", "CROSSING": "XING", "CROSSINGS": "XING",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}


def _normalize_street(street: str) -> str:
    """Uppercase, collapse whitespace, strip punctuation, abbreviate
    spelled-out suffix/directional words. Output aligns with TCAD's
    `Street Number + Street Name` concatenation format (e.g.
    `'1512 W 9TH ST'`)."""
    if not street:
        return ""
    import re as _re
    s = street.upper()
    s = _re.sub(r"[.,]", "", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return " ".join(_STREET_TOKEN_MAP.get(tok, tok) for tok in s.split(" "))


def _cache_path(filename: str) -> Path:
    base = Path(config.TRAVIS_TAX_CACHE_DIR)
    base.mkdir(parents=True, exist_ok=True)
    return base / filename


def _is_fresh(path: Path, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl_hours * 3600


def _download(url: str, dest: Path) -> None:
    logger.info("Downloading %s → %s", url, dest)
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with open(tmp, "wb") as f:
        total = 0
        for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
            f.write(chunk)
            total += len(chunk)
    os.replace(tmp, dest)
    logger.info("  Saved %.1f MB to %s", total / (1 << 20), dest)


def download_if_stale(force: bool = False) -> tuple[Path, Path]:
    """Download both CSVs if missing or older than TTL. Returns (delq, cur) paths."""
    ttl = config.TRAVIS_TAX_CACHE_TTL_HOURS
    delq = _cache_path("TaxDelqOpenData.csv")
    cur = _cache_path("TaxCurOpenData.csv")

    if force or not _is_fresh(delq, ttl):
        _download(config.TRAVIS_TAX_DELINQUENT_CSV, delq)
    if force or not _is_fresh(cur, ttl):
        _download(config.TRAVIS_TAX_CURRENT_CSV, cur)
    return delq, cur


def _normalize_last(name: str) -> str:
    """Pull the last token of an owner name as the index key.

    Handles both "JOHN SMITH" (FIRST LAST) and "SMITH JOHN" (LAST FIRST, tax
    roll format). The last token of either form is always the last name — for
    "SMITH JOHN" that's "JOHN"... which is wrong. So: strip suffixes, then if
    the first token is an ALL-CAPS plausible surname (no common first names),
    prefer it. Simpler fallback: try both the first and last token as keys.
    """
    if not name:
        return ""
    # Strip suffixes
    clean = name.upper().strip()
    for suf in (" JR", " SR", " II", " III", " IV", " MD", " DDS", " ESQ"):
        if clean.endswith(suf):
            clean = clean[: -len(suf)].strip()
    # Return first token — tax rolls use LAST FIRST format, and TaxCurOpenData's
    # NAMELF column explicitly documents LAST FIRST. We index the last name there.
    parts = clean.split()
    return parts[0] if parts else ""


def _load_delq(
    path: Path,
    idx: dict[str, list[dict]],
    addr_idx: dict[tuple[str, str], dict],
    parcel_idx: dict[str, dict | str],
) -> int:
    """Parse TaxDelqOpenData.csv — has true situs address columns and the
    Delinquent Total + 1st Year Delinquent fields used by CAD-LIFT."""
    from datetime import datetime as _dt

    from scrapers.travis_texdel_cleaner import city_for_zip as _city_for_zip

    count = 0
    this_year = _dt.now().year
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(
            schema.sanitize_csv_lines(f, source="TaxDelqOpenData.csv")
        )
        # Header changed to raw mainframe codes on 2026-08-05; rows are renamed
        # back to canonical labels. Unlike the scraper we log-and-continue —
        # the Cur file below still carries ~98% of the index, and losing the
        # situs overlay beats losing all 471K records.
        try:
            schema.assert_delq_schema(reader.fieldnames)
        except schema.TravisSchemaError as e:
            logger.error("%s — skipping delinquent-situs overlay", e)
            return 0
        for raw_row in reader:
            row = schema.normalize_delq_row(raw_row)
            owner = (row.get("Owner Name") or "").strip()
            if not owner:
                continue
            key = _normalize_last(owner)
            if not key:
                continue
            street_num = (row.get("Street Number") or "").strip()
            street_name = (row.get("Street Name") or "").strip()
            situs = f"{street_num} {street_name}".strip()
            if not situs or not street_num:
                continue  # vacant land / missing situs

            # Delinquency fields used by tax_enricher.enrich_tax_delinquency()
            delq_total = (row.get("Delinquent Total") or "").strip()
            first_year_raw = (row.get("1st Year Delinquent") or "").strip()
            years_delinquent = ""
            if first_year_raw.isdigit():
                y = this_year - int(first_year_raw)
                if 0 <= y <= 100:
                    years_delinquent = str(y)

            zip5 = (row.get("Property Zip") or "").strip()[:5]
            record = {
                "fullname": owner,
                "situsaddress": situs,
                # Property city is not in the delinquent CSV — resolve it from
                # the property ZIP. Blanket "AUSTIN" mislabelled every Del
                # Valle / Pflugerville / Manor / Leander situs in the index.
                "scity": _city_for_zip(zip5).upper(),
                "sstate": "TX",
                "szip": zip5,
                "quickrefid": (row.get("Account #") or "").strip(),
                "totalpropmktvalue": (row.get("Appraisal Value") or "").strip(),
                "propertytypedesc": (row.get("Property Type Code") or "").strip(),
                "delinquent_total": delq_total,
                "years_delinquent": years_delinquent,
                "source": "delinquent_situs",
            }
            idx.setdefault(key, []).append(record)

            # Address index — authoritative situs, always wins over current_mailing
            addr_key = (_normalize_street(situs), zip5)
            addr_idx[addr_key] = record

            # Parcel index (authoritative)
            _add_parcel(parcel_idx, record)

            count += 1
    return count


def _load_cur(
    path: Path,
    idx: dict[str, list[dict]],
    addr_idx: dict[tuple[str, str], dict],
    parcel_idx: dict[str, dict | str],
) -> int:
    """Parse TaxCurOpenData.csv — owner + mailing address only, no situs.

    Mailing address is used as situs (the decedent's home IS their property
    for individual-owned parcels — the vast majority of probate candidates).
    Records are tagged `source="current_mailing"` so scoring can deprioritize
    them when a situs-tagged match also exists.

    Populates the address index only when no delinquent-situs record already
    owns the key — delinquent data always wins.
    """
    count = 0
    # The Cur file has very long legal descriptions; bump csv field size limit.
    csv.field_size_limit(_FIELD_LIMIT)
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        # sanitize_csv_lines is what keeps a single malformed county row from
        # zeroing the whole index — an unbalanced quote here makes csv swallow
        # the rest of a 200 MB file into one field and blow _FIELD_LIMIT.
        reader = csv.DictReader(
            schema.sanitize_csv_lines(f, source="TaxCurOpenData.csv")
        )
        for row in reader:
            owner = (row.get("NAMELF") or "").strip()
            if not owner:
                continue
            key = _normalize_last(owner)
            if not key:
                continue
            # Mailing address lives in MAILINGADDRESS for some rows and ADDRESS1
            # for others (the two are mutually-exclusive in this feed). Fall back
            # to ADDRESS1 so we don't drop owners like "LIEN TRAN INVESTMENT TRUST"
            # whose address sits only in ADDRESS1.
            mailing = (row.get("MAILINGADDRESS") or row.get("ADDRESS1") or "").strip()
            # 9-digit zip → 5-digit
            zipc = (row.get("ZIPCODE") or "").strip()
            if len(zipc) == 9 and zipc.isdigit():
                zipc = zipc[:5]
            state = (row.get("STATE") or "").strip().upper()
            record = {
                "fullname": owner,
                "situsaddress": mailing,
                "scity": (row.get("CITY") or "").strip().upper() or "AUSTIN",
                "sstate": state or "TX",
                "szip": zipc,
                "quickrefid": (row.get("PARCEL") or "").strip() or "",
                "totalpropmktvalue": "",
                "propertytypedesc": "",
                "delinquent_total": "",
                "years_delinquent": "",
                "source": "current_mailing",
            }
            # Parcel index gets EVERY owner with a parcel — even when BOTH address
            # fields are blank (we only need the owner name for the parcel path;
            # the formatter falls the mailing back to the property address). This
            # is what recovers the "not in roll" code-violation records.
            _add_parcel(parcel_idx, record)
            # Name + address indexes need a real mailing address, and keep the
            # TX-only heuristic (a probate decedent's mailing is assumed to be
            # their in-county home, so out-of-state mailings shouldn't seed them).
            if not mailing:
                continue
            if state and state != "TX":
                continue
            idx.setdefault(key, []).append(record)
            # Only populate address index if delinquent load didn't already.
            addr_key = (_normalize_street(mailing), zipc)
            addr_idx.setdefault(addr_key, record)
            count += 1
    return count


def load_index(force_download: bool = False) -> dict[str, list[dict]]:
    """Return the lazy-built owner-name index, downloading CSVs if stale.

    Also populates the sibling address index (see `search_by_address`).
    """
    global _INDEX, _ADDR_INDEX, _PARCEL_INDEX, _STREET_FALLBACK
    if _INDEX is not None and not force_download:
        return _INDEX
    _STREET_FALLBACK = None  # derived from _ADDR_INDEX — rebuild with it
    delq_path, cur_path = download_if_stale(force=force_download)
    idx: dict[str, list[dict]] = {}
    addr_idx: dict[tuple[str, str], dict] = {}
    parcel_idx: dict[str, dict | str] = {}
    logger.info("Loading Travis tax delinquent CSV into index…")
    n_delq = _load_delq(delq_path, idx, addr_idx, parcel_idx)
    logger.info("  %d delinquent-situs records indexed", n_delq)
    logger.info("Loading Travis tax current CSV into index (large file, ~60s)…")
    n_cur = _load_cur(cur_path, idx, addr_idx, parcel_idx)
    logger.info("  %d current-mailing records indexed", n_cur)
    _INDEX = idx
    _ADDR_INDEX = addr_idx
    _PARCEL_INDEX = parcel_idx
    n_ambig = sum(1 for v in parcel_idx.values() if v == "__AMBIG__")
    logger.info(
        "Travis tax cache ready: %d name keys, %d address keys, %d parcel keys "
        "(%d ambiguous), %d total records",
        len(idx), len(addr_idx), len(parcel_idx), n_ambig,
        sum(len(v) for v in idx.values()),
    )
    return idx


def search_by_name(last_name: str, first_name: str = "") -> list[dict]:
    """Return candidate property dicts matching `last_name` (exact key match).

    The caller (`cad_lookup.lookup_property_by_name`) applies fuzzy scoring
    on the full name using the same scoring pipeline as WCAD — we just hand
    back the bucket. `first_name` is unused at this level but accepted to
    match the WCAD signature.
    """
    idx = load_index()
    key = last_name.strip().upper()
    if not key:
        return []
    # Probe both directions: tax rolls use LAST FIRST; caller may pass either.
    # We indexed on first-token which for LAST-FIRST format IS the last name.
    return idx.get(key, [])


def search_by_address(street: str, zip5: str) -> dict | None:
    """Return the property record matching a normalized street+ZIP key.

    The delinquent CSV is authoritative — if a delinquent_situs record
    exists at this address, it wins over a current_mailing record. Returns
    None when no TCAD record matches.

    When the exact (street, zip) key misses — or the caller has no ZIP at
    all — falls back to a street-only match, accepted ONLY when the full
    "NUM STREET" string resolves to a single Austin-area ZIP (786xx/787xx).
    Reverse-geocoded ZIPs are routinely wrong (Nominatim returns PO-box ZIPs
    like 78715 for 78745 streets), and the ZIP guard keeps out-of-county
    MAILING-address keys (absentee owners in Houston/Dallas) from matching.
    """
    global _ADDR_INDEX
    if _ADDR_INDEX is None:
        load_index()
    if _ADDR_INDEX is None:  # still None = load failed
        return None
    street_key = _normalize_street(street)
    if not street_key:
        return None
    zip5 = (zip5 or "").strip()[:5]
    if zip5:
        hit = _ADDR_INDEX.get((street_key, zip5))
        if hit:
            return hit
    return _street_fallback(street_key)


def _street_fallback(street_key: str) -> dict | None:
    """Street-only lookup: unique Austin-area ZIP for this exact NUM+STREET."""
    global _STREET_FALLBACK
    if _STREET_FALLBACK is None:
        fb: dict[str, list[str]] = {}
        for st, zp in _ADDR_INDEX:
            if zp.startswith(("786", "787")):
                fb.setdefault(st, []).append(zp)
        _STREET_FALLBACK = fb
    zips = set(_STREET_FALLBACK.get(street_key) or [])
    if len(zips) != 1:
        return None
    return _ADDR_INDEX.get((street_key, zips.pop()))


def search_by_parcel(parcel_id: str) -> dict | None:
    """Return the property record for a parcel id (exact 10-digit geo-id match).

    Accepts either Austin's 10-digit `parcelid` or TCAD's 14-digit `PARCEL`
    (both reduce to the same 10-digit geo key). Returns None when the parcel is
    unknown or maps to an ambiguous condo/apartment geo shared by multiple
    owners — the caller should fall back to the address lookup in that case."""
    global _PARCEL_INDEX
    if _PARCEL_INDEX is None:
        load_index()
    if _PARCEL_INDEX is None:  # still None = load failed
        return None
    key = _normalize_parcel(parcel_id)
    if not key:
        return None
    hit = _PARCEL_INDEX.get(key)
    return hit if isinstance(hit, dict) else None

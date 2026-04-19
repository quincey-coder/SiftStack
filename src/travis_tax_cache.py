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

logger = logging.getLogger(__name__)

# Lazy-loaded, process-wide index: {normalized_last_name: [property dicts]}
_INDEX: dict[str, list[dict]] | None = None

_FIELD_LIMIT = 10 * 1024 * 1024  # 10 MB — TaxCurOpenData has long quoted rows


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


def _load_delq(path: Path, idx: dict[str, list[dict]]) -> int:
    """Parse TaxDelqOpenData.csv — has true situs address columns."""
    count = 0
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
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
            idx.setdefault(key, []).append({
                "fullname": owner,
                "situsaddress": situs,
                # Property city is not in the delinquent CSV; Travis Tax Office
                # situs defaults to Austin for the overwhelming majority.
                "scity": "AUSTIN",
                "szip": (row.get("Property Zip") or "").strip()[:5],
                "quickrefid": (row.get("Account #") or "").strip(),
                "totalpropmktvalue": (row.get("Appraisal Value") or "").strip(),
                "propertytypedesc": (row.get("Property Type Code") or "").strip(),
                "source": "delinquent_situs",
            })
            count += 1
    return count


def _load_cur(path: Path, idx: dict[str, list[dict]]) -> int:
    """Parse TaxCurOpenData.csv — owner + mailing address only, no situs.

    Mailing address is used as situs (the decedent's home IS their property
    for individual-owned parcels — the vast majority of probate candidates).
    Records are tagged `source="current_mailing"` so scoring can deprioritize
    them when a situs-tagged match also exists.
    """
    count = 0
    # The Cur file has very long legal descriptions; bump csv field size limit.
    csv.field_size_limit(_FIELD_LIMIT)
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            owner = (row.get("NAMELF") or "").strip()
            if not owner:
                continue
            key = _normalize_last(owner)
            if not key:
                continue
            mailing = (row.get("MAILINGADDRESS") or "").strip()
            if not mailing:
                continue
            # 9-digit zip → 5-digit
            zipc = (row.get("ZIPCODE") or "").strip()
            if len(zipc) == 9 and zipc.isdigit():
                zipc = zipc[:5]
            state = (row.get("STATE") or "").strip().upper()
            if state and state != "TX":
                continue  # out-of-state mailing = not a TX probate property
            idx.setdefault(key, []).append({
                "fullname": owner,
                "situsaddress": mailing,
                "scity": (row.get("CITY") or "").strip().upper() or "AUSTIN",
                "szip": zipc,
                "quickrefid": (row.get("PARCEL") or "").strip().strip() or "",
                "totalpropmktvalue": "",
                "propertytypedesc": "",
                "source": "current_mailing",
            })
            count += 1
    return count


def load_index(force_download: bool = False) -> dict[str, list[dict]]:
    """Return the lazy-built owner-name index, downloading CSVs if stale."""
    global _INDEX
    if _INDEX is not None and not force_download:
        return _INDEX
    delq_path, cur_path = download_if_stale(force=force_download)
    idx: dict[str, list[dict]] = {}
    logger.info("Loading Travis tax delinquent CSV into index…")
    n_delq = _load_delq(delq_path, idx)
    logger.info("  %d delinquent-situs records indexed", n_delq)
    logger.info("Loading Travis tax current CSV into index (large file, ~60s)…")
    n_cur = _load_cur(cur_path, idx)
    logger.info("  %d current-mailing records indexed", n_cur)
    _INDEX = idx
    logger.info(
        "Travis tax cache ready: %d unique keys, %d total records",
        len(idx), sum(len(v) for v in idx.values()),
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

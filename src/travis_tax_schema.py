"""Travis Tax Office bulk-CSV schema shim.

The Tax Office changed its `TaxDelqOpenData.csv` export on **2026-08-05**:
same 31 columns in the same order, but the friendly labels were replaced with
raw mainframe field codes (`Account #` → `ACCOUNT#`, `Owner Name` →
`OWNERSNAME`, `1st Year Delinquent` → `SINCE`, …). The same export change
also introduced fixed-width space padding (`MILTON<29 spaces>ST   W`) and
2-decimal money with a bare leading dot (`.00`).

Every consumer in this repo reads the friendly labels, so rather than rewrite
them all, incoming rows are renamed back to the canonical labels here.
Both layouts are accepted.

`assert_delq_schema()` raises when a header matches NEITHER layout. That
matters: the Aug-5 change cost two days of silent zero-record runs because the
scraper's `no_apn` guard swallowed all 9,082 renamed rows without complaint —
the run still exited 0. A hard failure is the point.

`sanitize_csv_lines()` guards the other half of the same incident: the Tax
Office also began emitting rows with an unbalanced quote, which makes the csv
module swallow the rest of the file into one field.
"""

import logging
import re

logger = logging.getLogger(__name__)


# ── TaxDelqOpenData.csv column aliases ───────────────────────────────
# Mainframe field code (2026-08-05 onward) → canonical friendly label
# (the pre-2026-08-05 header, which the rest of the codebase reads).
# Listed in source-column order.
DELQ_ALIASES: dict[str, str] = {
    "ACCOUNT#": "Account #",
    "ROLL": "Last Tax Roll Year",
    "SPTC": "Property Type Code",
    "LANDV": "Land Value",
    "IMPV": "Improvement Value",
    "AGMKT": "Ag Market",
    "APPVAL": "Appraisal Value",
    "AGTAX": "Ag Taxable Value",
    "ASDVAL": "Assessed Value",
    "XMPT": "Exemptions",
    "DELQTOT": "Delinquent Total",
    "CURRTOT": "Current Year Total",
    "TOTDUE": "Total Due",
    "SEQD": "Sequence #",
    "SINCE": "1st Year Delinquent",
    # Header literal is `"00016"`; csv strips the quotes before we see it.
    "00016": "Cause #",
    "JUDGMNT": "Judgement Date",
    "BNKRPT#": "Bankruptcy Number",
    "OWNID": "Owner ID",
    "OWNERSNAME": "Owner Name",
    "ADDRS1": "Address 1",
    "ADDRS2": "Address 2",
    "ADDRS3": "Address 3",
    "CITY": "City",
    "STATE": "State",
    "ZIPCODE": "Zip Code",
    "COUNTRY": "Country",
    "STR#": "Street Number",
    "STREETNAME": "Street Name",
    "PROPZIP": "Property Zip",
    "LEGAL": "Legal Description",
}

# Canonical columns without which the file is useless to us. Kept small and
# load-bearing — a header that resolves all of these is a header we can parse.
DELQ_REQUIRED: tuple[str, ...] = (
    "Account #",
    "Owner Name",
    "Street Number",
    "Street Name",
    "Property Zip",
    "Delinquent Total",
    "1st Year Delinquent",
)

_WS_RE = re.compile(r"\s+")

# Cap the per-file log spam from sanitize_csv_lines; a summary line follows.
_MAX_LOGGED_BAD_LINES = 5


class TravisSchemaError(ValueError):
    """A Travis Tax Office CSV header matched no layout we know about."""


def canonical_delq_key(name: str | None) -> str | None:
    """Map one raw header cell to its canonical friendly label.

    Whitespace is stripped first — the pre-2026-08-05 header shipped
    `'Last Tax Roll Year '` with a trailing space, which silently defeated
    every lookup that spelled it without one.
    """
    if name is None:
        return None
    key = name.strip()
    return DELQ_ALIASES.get(key, key)


def assert_delq_schema(fieldnames, source: str = "TaxDelqOpenData.csv") -> None:
    """Raise `TravisSchemaError` unless the header resolves every required column.

    Call this once per file, right after building the reader. Silent-zero is
    the failure mode this exists to prevent — see the module docstring.
    """
    canon = {canonical_delq_key(f) for f in (fieldnames or []) if f}
    missing = [c for c in DELQ_REQUIRED if c not in canon]
    if missing:
        raise TravisSchemaError(
            f"{source}: unrecognized header — cannot resolve {missing}. "
            f"Travis Tax Office likely changed the export again; add the new "
            f"column names to travis_tax_schema.DELQ_ALIASES. "
            f"Got: {sorted(f for f in (fieldnames or []) if f)}"
        )


def normalize_delq_row(row: dict) -> dict:
    """Rename a raw CSV row's keys to canonical labels and clean its values.

    Values get interior whitespace collapsed as well as trimmed — the post-
    2026-08-05 export pads `Street Name` to fixed width, so an uncollapsed
    value ships as `'1012 MILTON<29 spaces>ST   W'` in the property address.
    """
    out: dict = {}
    for key, value in row.items():
        canon = canonical_delq_key(key)
        if canon is None:
            continue  # csv.DictReader restkey — surplus columns, not ours
        if isinstance(value, str):
            value = _WS_RE.sub(" ", value).strip()
        out[canon] = value
    return out


def sanitize_csv_lines(lines, source: str = "CSV"):
    """Yield CSV lines, neutralizing any row with an unbalanced quote count.

    Travis began emitting malformed rows on 2026-08-05 — one TaxCurOpenData.csv
    row carries a ZIPCODE of three literal quote characters followed by '78',
    which opens a quoted field that never closes. The csv module then swallows
    the remaining ~170 MB into a single field and dies on the field-size limit,
    taking the entire 471K-record index with it. Stripping the quotes from just
    that physical line costs one row and saves the other 492,802.

    Balanced-but-embedded quotes (ZHANG ZHIGANG "TIM", LOT 1 * ("B" ...)) are
    left alone — csv already handles those correctly.
    """
    bad = 0
    for lineno, line in enumerate(lines, start=1):
        if line.count('"') % 2:
            bad += 1
            if bad <= _MAX_LOGGED_BAD_LINES:
                logger.warning(
                    "%s line %d: unbalanced quote — stripping quotes from this "
                    "row so the rest of the file still parses",
                    source, lineno,
                )
            line = line.replace('"', "")
        yield line
    if bad > _MAX_LOGGED_BAD_LINES:
        logger.warning(
            "%s: %d rows total had unbalanced quotes (first %d logged above)",
            source, bad, _MAX_LOGGED_BAD_LINES,
        )

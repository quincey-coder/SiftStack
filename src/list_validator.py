"""Post-generation output validator — the gate before Drive + Slack + CRM upload.

Runs AFTER every output CSV for a run is written and BEFORE anything is published
(Google Drive upload, Slack notification, DataSift/RAWPIPE upload). It loops over
the actual files created this run and checks:

  1. STRUCTURE (hard / blocking)
     - file exists, is non-empty, parses as CSV, has a header + >=1 data row
     - no ragged rows (every row's field count matches the header)
     - header parity: a DataSift-format file MUST match datasift_formatter's
       canonical column list exactly (else the upload column-mapper misaligns)

  2. DATA QUALITY (soft / reported, non-blocking unless STRICT)
     - Addresses: blank street, non-TX state, malformed ZIP
     - Names: ALL-CAPS red flag, entity words in a person field, First==Last,
       OCR corruption (a digit welded into a name, e.g. "Alvarad0"), stray digits,
       and rows with neither a person nor a business name (unmarketable)
     - County outside the operating set (Bell/Travis/Williamson)
     - exact duplicate property rows

Severity model: any ERROR finding BLOCKS publishing — the pipeline skips Drive,
Slack-success and CRM upload, and instead fires a Slack alert with the findings.
WARN findings are surfaced (Slack + a written report) but do not block, unless
config.LIST_VALIDATOR_STRICT makes warnings blocking too.

Toggles (config / env):
  LIST_VALIDATOR_ENABLED  (default true)  — master on/off
  LIST_VALIDATOR_STRICT   (default false) — treat WARN as blocking too
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ERROR = "error"
WARN = "warn"

# Counties this operation covers (see CLAUDE.md). Anything else is suspect.
KNOWN_COUNTIES = {"bell", "travis", "williamson"}

# Words that mean a "person" name field is really an entity → should be Business
# Name. Mirrors the NAMELF guidance in CLAUDE.md.
_ENTITY_RE = re.compile(
    r"\b(LLC|L\.?L\.?C|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|TRUST|ESTATE|"
    r"CHURCH|MINISTRIES|LP|LLP|LTD|BANK|ASSN|ASSOCIATION|HOA|PARTNERS|HOLDINGS|"
    r"PROPERTIES|INVESTMENTS|AUTHORITY|COUNTY|CITY OF|FUND|GROUP|ENTERPRISES|"
    r"MANAGEMENT|CAPITAL|CONTRACTING|CONSTRUCTION|REALTY|RENTALS|SALES|HOMES|VENTURES)\b",
    re.I,
)
# A digit fused into an alpha run — classic OCR (O->0, l->1) or bad-data tell.
_OCR_DIGIT_RE = re.compile(r"[A-Za-z]\d|\d[A-Za-z]")
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

# Max sample values recorded per finding (keeps the report + Slack message small).
MAX_SAMPLES = 6


@dataclass
class Finding:
    severity: str
    kind: str
    message: str
    count: int = 1
    samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity, "kind": self.kind, "message": self.message,
            "count": self.count, "samples": self.samples[:MAX_SAMPLES],
        }


@dataclass
class FileReport:
    path: str
    rows: int = 0
    schema: str = "unknown"
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == ERROR)

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.severity == WARN)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "rows": self.rows, "schema": self.schema,
            "errors": self.errors, "warnings": self.warnings,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class ValidationReport:
    file_reports: list[FileReport] = field(default_factory=list)
    strict: bool = False

    @property
    def total_errors(self) -> int:
        return sum(fr.errors for fr in self.file_reports)

    @property
    def total_warnings(self) -> int:
        return sum(fr.warnings for fr in self.file_reports)

    @property
    def blocked(self) -> bool:
        if self.total_errors:
            return True
        return self.strict and self.total_warnings > 0

    @property
    def ok(self) -> bool:
        return not self.blocked

    def to_dict(self) -> dict:
        return {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": len(self.file_reports),
            "total_errors": self.total_errors,
            "total_warnings": self.total_warnings,
            "blocked": self.blocked,
            "strict": self.strict,
            "file_reports": [fr.to_dict() for fr in self.file_reports],
        }


# ── config helpers (lazy so the module imports even without config) ──────
def _cfg_bool(name: str, default: bool) -> bool:
    try:
        import config
        return bool(getattr(config, name, default))
    except Exception:
        return default


def _canonical_datasift_columns() -> list[str] | None:
    try:
        from datasift_formatter import DATASIFT_COLUMNS
        return list(DATASIFT_COLUMNS)
    except Exception:
        return None


# The canonical entity/owner column is "FULL NAME/COMPANY/TRUST" (DataSift's
# uploader label); the older internal name was "Business Name". Alias so
# pre-rename masters/archived CSVs still pass header-parity and resolve the
# column. (The DM/deep-prospecting columns keep their original DataSift
# custom-field labels — no alias needed there.)
_HEADER_ALIASES = {
    "Business Name": "FULL NAME/COMPANY/TRUST",
}


def _norm_header(cols: list[str]) -> list[str]:
    return [_HEADER_ALIASES.get(c, c) for c in cols]


# ── column resolution across the two known schemas ──────────────────────
def _resolve_columns(header: list[str]) -> dict[str, str | None]:
    """Map logical field → actual header name present in this file (or None)."""
    hset = set(header)

    def pick(*cands):
        for c in cands:
            if c in hset:
                return c
        return None

    return {
        "street": pick("Property Street Address", "address", "property_street"),
        "city": pick("Property City", "city", "property_city"),
        "state": pick("Property State", "state", "property_state"),
        "zip": pick("Property ZIP Code", "zip", "property_zip", "property_postal_code"),
        "first": pick("Owner First Name", "first_name"),
        "last": pick("Owner Last Name", "last_name"),
        "business": pick("Business Name", "FULL NAME/COMPANY/TRUST", "business_name"),
        "owner_full": pick("owner_name", "full_name", "Owner Name"),
        "county": pick("County", "county"),
        "parcel": pick("Parcel ID", "parcel_id", "parcel"),
    }


def _detect_schema(header: list[str]) -> str:
    hnorm = set(_norm_header(header))
    canon = _canonical_datasift_columns()
    if canon and set(canon).issubset(hnorm):
        return "datasift"
    if {"Property Street Address", "Owner First Name", "Business Name"} <= hnorm:
        return "datasift"
    if {"address", "owner_name", "notice_type"} <= set(header):
        return "sift"
    return "unknown"


# ── per-file validation ──────────────────────────────────────────────────
def validate_file(path: Path) -> FileReport:
    path = Path(path)
    fr = FileReport(path=str(path))

    # ---- structural: existence + readability ----
    if not path.exists():
        fr.findings.append(Finding(ERROR, "missing_file", f"File does not exist: {path.name}"))
        return fr
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        fr.findings.append(Finding(ERROR, "unreadable", f"Cannot read file: {e}"))
        return fr
    if not text.strip():
        fr.findings.append(Finding(ERROR, "empty_file", "File is empty (0 bytes of content)"))
        return fr

    try:
        reader = csv.reader(text.splitlines())
        header = next(reader, None)
    except Exception as e:
        fr.findings.append(Finding(ERROR, "unparseable", f"CSV parse error in header: {e}"))
        return fr
    if not header:
        fr.findings.append(Finding(ERROR, "no_header", "No header row found"))
        return fr

    fr.schema = _detect_schema(header)

    # ---- header parity for DataSift-format files ----
    canon = _canonical_datasift_columns()
    hnorm = _norm_header(header)
    if fr.schema == "datasift" and canon is not None and hnorm != canon:
        missing = [c for c in canon if c not in hnorm]
        extra = [_norm_header([c])[0] for c in header if _norm_header([c])[0] not in canon]
        detail = []
        if missing:
            detail.append(f"missing {len(missing)}: {missing[:5]}")
        if extra:
            detail.append(f"unexpected {len(extra)}: {extra[:5]}")
        if not detail:  # same set, wrong order
            detail.append("column order differs from canonical DataSift header")
        fr.findings.append(Finding(
            ERROR, "header_mismatch",
            "DataSift header does not match the uploader's canonical columns — "
            + "; ".join(detail),
        ))

    # ---- read data rows with DictReader (handles quoted multiline fields) ----
    try:
        drows = list(csv.DictReader(text.splitlines()))
    except Exception as e:
        fr.findings.append(Finding(ERROR, "unparseable", f"CSV parse error in body: {e}"))
        return fr
    fr.rows = len(drows)

    if fr.rows == 0:
        fr.findings.append(Finding(ERROR, "no_rows", "File has a header but zero data rows"))
        return fr

    # ragged rows: DictReader stuffs surplus fields under None (restkey)
    ragged = sum(1 for r in drows if None in r and r[None] not in (None, [], [""]))
    if ragged:
        fr.findings.append(Finding(
            ERROR, "ragged_rows",
            f"{ragged} row(s) have more fields than the header (misaligned columns)",
            count=ragged,
        ))

    cols = _resolve_columns(header)
    _check_addresses(drows, cols, fr)
    _check_names(drows, cols, fr)
    _check_county(drows, cols, fr)
    _check_duplicates(drows, cols, fr)
    return fr


def _add(fr: FileReport, sev: str, kind: str, msg: str, samples: list[str]):
    if samples:
        fr.findings.append(Finding(sev, kind, msg, count=len(samples), samples=samples[:MAX_SAMPLES]))


def _check_addresses(rows, cols, fr):
    c_street, c_state, c_zip = cols["street"], cols["state"], cols["zip"]
    if c_street:
        blank = [f"row {i+2}" for i, r in enumerate(rows) if not (r.get(c_street) or "").strip()]
        if blank and len(blank) == len(rows):
            fr.findings.append(Finding(ERROR, "all_blank_street",
                                       "Every row has a blank property street address"))
        else:
            _add(fr, WARN, "blank_street",
                 f"{len(blank)} row(s) missing a property street address", blank)
    if c_state:
        bad = [f"{(r.get(c_state) or '').strip()} (row {i+2})"
               for i, r in enumerate(rows)
               if (r.get(c_state) or "").strip() and (r.get(c_state) or "").strip().upper() != "TX"]
        _add(fr, WARN, "non_tx_state", f"{len(bad)} row(s) with a non-TX property state", bad)
    if c_zip:
        bad = [f"{(r.get(c_zip) or '').strip()!r} (row {i+2})"
               for i, r in enumerate(rows)
               if (r.get(c_zip) or "").strip() and not _ZIP_RE.match((r.get(c_zip) or "").strip())]
        _add(fr, WARN, "malformed_zip", f"{len(bad)} row(s) with a malformed ZIP", bad)


def _is_upper_word(s: str) -> bool:
    return s.isupper() and len(re.sub(r"[^A-Za-z]", "", s)) > 1


def _check_names(rows, cols, fr):
    c_first, c_last, c_biz, c_full = cols["first"], cols["last"], cols["business"], cols["owner_full"]

    allcaps, entity, feq, ocr, digits, nameless = [], [], [], [], [], []
    for i, r in enumerate(rows):
        rownum = i + 2
        fn = (r.get(c_first) or "").strip() if c_first else ""
        ln = (r.get(c_last) or "").strip() if c_last else ""
        biz = (r.get(c_biz) or "").strip() if c_biz else ""
        full = (r.get(c_full) or "").strip() if c_full else ""
        person = f"{fn} {ln}".strip() if (fn or ln) else full

        if not person and not biz:
            nameless.append(f"row {rownum}")
            continue
        if not person:
            continue  # entity-only row: business name checks are fine

        if _is_upper_word(fn) or _is_upper_word(ln) or (full and _is_upper_word(full)):
            allcaps.append(f"{person} (row {rownum})")
        if _ENTITY_RE.search(person):
            entity.append(f"{person} (row {rownum})")
        if fn and ln and fn.lower() == ln.lower():
            feq.append(f"{person} (row {rownum})")
        if _OCR_DIGIT_RE.search(person):
            ocr.append(f"{person} (row {rownum})")
        elif re.search(r"\d", person):
            digits.append(f"{person} (row {rownum})")

    _add(fr, WARN, "all_caps_name",
         f"{len(allcaps)} ALL-CAPS owner name(s) — may have bypassed normalization / be reversed", allcaps)
    _add(fr, WARN, "entity_as_person",
         f"{len(entity)} business/entity name(s) in a person field — should be Business Name", entity)
    _add(fr, WARN, "first_equals_last",
         f"{len(feq)} row(s) where Owner First == Owner Last (positional-split artifact)", feq)
    _add(fr, WARN, "ocr_in_name",
         f"{len(ocr)} owner name(s) with a digit fused into the text (OCR corruption)", ocr)
    _add(fr, WARN, "digits_in_name",
         f"{len(digits)} owner name(s) containing stray digits", digits)
    _add(fr, WARN, "no_owner_or_business",
         f"{len(nameless)} row(s) with neither an owner nor a business name (unmarketable)", nameless)


def _check_county(rows, cols, fr):
    c_county = cols["county"]
    if not c_county:
        return
    bad = [f"{(r.get(c_county) or '').strip()!r} (row {i+2})"
           for i, r in enumerate(rows)
           if (r.get(c_county) or "").strip() and (r.get(c_county) or "").strip().lower() not in KNOWN_COUNTIES]
    _add(fr, WARN, "unknown_county", f"{len(bad)} row(s) with a county outside Bell/Travis/Williamson", bad)


def _check_duplicates(rows, cols, fr):
    """Flag genuinely-duplicate properties.

    When a Parcel ID column is present it is the truth: a parcel is one unique
    property, so a repeated parcel = a real duplicate. Only fall back to
    (street, zip, owner) when there's no parcel — that keeps legitimately-distinct
    parcels that share a street (e.g. a builder's adjacent vacant lots with no
    house number) from being flagged.
    """
    c_street, c_zip, c_parcel = cols["street"], cols["zip"], cols["parcel"]
    c_first, c_last, c_biz, c_full = cols["first"], cols["last"], cols["business"], cols["owner_full"]

    use_parcel = c_parcel and any((r.get(c_parcel) or "").strip() for r in rows)
    seen: dict = {}
    for r in rows:
        if use_parcel:
            pid = (r.get(c_parcel) or "").strip()
            if not pid:
                continue
            key = ("parcel", pid)
        else:
            if not c_street:
                return
            owner = (r.get(c_full) or "").strip().lower() if c_full else \
                f"{(r.get(c_first) or '').strip()} {(r.get(c_last) or '').strip()} {(r.get(c_biz) or '').strip()}".strip().lower()
            street = (r.get(c_street) or "").strip().lower()
            if not street:
                continue
            key = (street, (r.get(c_zip) or "").strip() if c_zip else "", owner)
        seen[key] = seen.get(key, 0) + 1
    label = "Parcel ID" if use_parcel else "property+owner"
    dups = [f"{k[-1]} ({n}x)" for k, n in seen.items() if n > 1]
    _add(fr, WARN, "duplicate_rows",
         f"{len(dups)} {label} value(s) appear more than once", dups)


# ── top-level: validate a set of files ───────────────────────────────────
def validate_files(paths: list) -> ValidationReport:
    strict = _cfg_bool("LIST_VALIDATOR_STRICT", False)
    report = ValidationReport(strict=strict)
    for p in paths:
        report.file_reports.append(validate_file(Path(p)))
    return report


# ── report formatting + persistence ──────────────────────────────────────
def format_report(report: ValidationReport, *, for_slack: bool = True) -> str:
    head = ":no_entry: *List validation FAILED — publish blocked*" if report.blocked \
        else (":warning: *List validation passed with warnings*" if report.total_warnings
              else ":white_check_mark: *List validation passed*")
    lines = [
        head,
        f"{len(report.file_reports)} file(s) · "
        f"{report.total_errors} error(s) · {report.total_warnings} warning(s)",
    ]
    for fr in report.file_reports:
        if not fr.findings and not for_slack:
            lines.append(f"• `{Path(fr.path).name}` — {fr.rows} rows — OK")
            continue
        if not fr.findings:
            continue
        lines.append(f"• `{Path(fr.path).name}` ({fr.rows} rows, {fr.errors}E/{fr.warnings}W)")
        for f in fr.findings:
            icon = "🚫" if f.severity == ERROR else "⚠️"
            samp = f"  e.g. {', '.join(f.samples[:3])}" if f.samples else ""
            lines.append(f"   {icon} [{f.kind}] {f.message}{samp}")
    if report.blocked:
        lines.append("*Drive upload, Slack summary and CRM upload were skipped.* "
                     "Fix the errors above and re-run.")
    return "\n".join(lines)


def write_report(report: ValidationReport, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    p = out_dir / f"list_validation_{ts}.json"
    p.write_text(json.dumps(report.to_dict(), indent=2))
    return p


# ── the pipeline gate ─────────────────────────────────────────────────────
def gate_outputs(
    paths: list,
    *,
    slack: bool = True,
    webhook_url: str | None = None,
    out_dir: Path | None = None,
    log=logger,
) -> tuple[bool, ValidationReport]:
    """Validate all output files created this run; decide whether to publish.

    Returns (publish_ok, report). Side effects:
      - always logs a one-line verdict + writes a JSON report (best-effort)
      - if blocked and `slack`, fires a Slack alert with the findings
    Callers gate Drive upload, Slack-success and CRM upload on `publish_ok`.
    """
    if not _cfg_bool("LIST_VALIDATOR_ENABLED", True):
        log.info("List validator disabled (LIST_VALIDATOR_ENABLED=false) — skipping gate")
        return True, ValidationReport()

    paths = [Path(p) for p in paths if p]
    report = validate_files(paths)

    # Persist a report for the audit/forensics trail (never fatal).
    try:
        if out_dir is None:
            import config
            out_dir = getattr(config, "OUTPUT_DIR", Path("output"))
        rp = write_report(report, Path(out_dir))
        log.info("Validation report written: %s", rp)
    except Exception as e:
        log.warning("Could not write validation report: %s", e)

    verdict = "BLOCKED" if report.blocked else ("warnings" if report.total_warnings else "clean")
    log.info(
        "List validation: %d file(s), %d error(s), %d warning(s) → %s",
        len(report.file_reports), report.total_errors, report.total_warnings, verdict,
    )

    if report.blocked and slack:
        try:
            from slack_notifier import _send_webhook
            _send_webhook(format_report(report, for_slack=True), webhook_url)
        except Exception as e:
            log.warning("Could not send validation-failure Slack alert: %s", e)

    return report.ok, report

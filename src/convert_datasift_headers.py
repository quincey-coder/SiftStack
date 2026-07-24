"""Convert archived 74-col DataSift CSVs to the 72-col v2 header contract.

The v2 contract (build 2026-07-08) renamed/merged the enrichment columns so
headers match the "Deceased & Heir Intelligence" custom-field labels 1:1 and
select values ship as exact option labels (case-sensitive match on import).
Archived CSVs written before that carry the old 74-col headers — run them
through this before uploading or merging into a master.

Changes applied:
  renames   Business Name → FULL NAME/COMPANY/TRUST, Decision Maker →
            Decision Maker (Name), DM 1 Source → DM Source, Signing Chain Count
            → Signatures to Close
  normalize DM Confidence → Decision-Maker Confidence, DM 1 Status →
            Decision-Maker Status (values → exact option labels)
  merges    DM 2/3 Name + Relationship → "DM 2/3 Name / Relationship"
            ("Name (relationship)"), Entity Contact + Entity Contact Role
            → "Entity Contact + Role"
  derived   Title Flag (from Data Flags: cad_life_estate/cad_et_al)
  values    select columns normalized to option labels (yes→Yes,
            verified_living→Verified Living, code_violation→Code Violation, …)

Every non-empty source cell is audited into its target column — the script
fails loudly on any would-be data loss.

Usage (from project root):
    python src/convert_datasift_headers.py output/lists-5 output/lists-6
    python src/convert_datasift_headers.py old.csv new_dir/
"""

import csv
import logging
import sys
from pathlib import Path

from datasift_formatter import (
    DATASIFT_COLUMNS, _select_label, _county_label, _title_flag, _name_rel,
    _NOTICE_TYPE_LABELS, _OWNER_DECEASED_LABELS, _DM_STATUS_LABELS, _CONFIDENCE_LABELS,
)

logger = logging.getLogger(__name__)

# Converts old DataSift-export names → our formatter's column names. The only
# entity/owner change is Business Name → FULL NAME/COMPANY/TRUST; the DM /
# deep-prospecting columns keep their DataSift custom-field labels.
_RENAMES = {
    "Business Name": "FULL NAME/COMPANY/TRUST",
    "Decision Maker": "Decision Maker (Name)",
    "DM 1 Source": "DM Source",
    "Signing Chain Count": "Signatures to Close",
}

# old column → (new column, normalizer)
_NORMALIZED = {
    "DM Confidence": ("Decision-Maker Confidence", lambda v: _select_label(v, _CONFIDENCE_LABELS)),
    "DM 1 Status": ("Decision-Maker Status", lambda v: _select_label(v, _DM_STATUS_LABELS)),
    "DM 2 Status": ("DM 2 Status", lambda v: _select_label(v, _DM_STATUS_LABELS)),
    "DM 3 Status": ("DM 3 Status", lambda v: _select_label(v, _DM_STATUS_LABELS)),
    "Owner Deceased": ("Owner Deceased", lambda v: _select_label(v, _OWNER_DECEASED_LABELS)),
    "Notice Type": ("Notice Type", lambda v: _select_label(v, _NOTICE_TYPE_LABELS)),
    "County": ("County", _county_label),
}

_MERGES = {
    "DM 2 Name / Relationship": ("DM 2 Name", "DM 2 Relationship"),
    "DM 3 Name / Relationship": ("DM 3 Name", "DM 3 Relationship"),
    "Entity Contact + Role": ("Entity Contact", "Entity Contact Role"),
}

_MERGE_SOURCES = {c for pair in _MERGES.values() for c in pair}


def _merge(name: str, rel: str) -> str:
    """_name_rel, but a relationship/role with no name still survives."""
    combined = _name_rel(name, rel)
    if not combined and (rel or "").strip():
        return rel.strip()
    return combined


def convert_row(old: dict) -> dict:
    new = {c: "" for c in DATASIFT_COLUMNS}
    for k, v in old.items():
        if k is None or k in _MERGE_SOURCES or k in _NORMALIZED:
            continue
        target = _RENAMES.get(k, k)
        if target in new:
            new[target] = v or ""
    for src, (target, fn) in _NORMALIZED.items():
        new[target] = fn(old.get(src, ""))
    for target, (name_col, rel_col) in _MERGES.items():
        new[target] = _merge(old.get(name_col, ""), old.get(rel_col, ""))
    new["Title Flag"] = _title_flag(old.get("Data Flags", ""))
    return new


def _audit_row(old: dict, new: dict, path: str, idx: int) -> list[str]:
    """Return loss descriptions for any non-empty old cell absent from its target."""
    losses = []
    for k, v in old.items():
        if k is None or not (v or "").strip():
            continue
        if k in _NORMALIZED:
            target = _NORMALIZED[k][0]
            ok = bool(new[target].strip())
        elif k in _MERGE_SOURCES:
            target = next(t for t, pair in _MERGES.items() if k in pair)
            ok = v.strip() in new[target]
        else:
            target = _RENAMES.get(k, k)
            ok = target in new and new[target] == v
        if not ok:
            losses.append(f"{path} row {idx}: {k!r}={v[:40]!r} lost (target {target!r})")
    return losses


def convert_file(src: Path, dest: Path) -> dict:
    losses: list[str] = []
    n = 0
    with open(src, newline="", encoding="utf-8-sig") as fin, \
         open(dest, "w", newline="", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for i, old in enumerate(reader, 1):
            new = convert_row(old)
            losses.extend(_audit_row(old, new, src.name, i))
            writer.writerow(new)
            n += 1
    return {"rows": n, "losses": losses}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src_arg, dest_dir = Path(sys.argv[1]), Path(sys.argv[2])
    files = sorted(src_arg.glob("*.csv")) if src_arg.is_dir() else [src_arg]
    if not files:
        logger.error("No CSVs found at %s", src_arg)
        return 1
    dest_dir.mkdir(parents=True, exist_ok=True)

    total_losses = 0
    for f in files:
        result = convert_file(f, dest_dir / f.name)
        for loss in result["losses"][:10]:
            logger.error("  LOSS: %s", loss)
        total_losses += len(result["losses"])
        logger.info("%s: %d rows → %s (%d loss issues)",
                    f.name, result["rows"], dest_dir / f.name, len(result["losses"]))
    if total_losses:
        logger.error("FAILED — %d cells would be lost; output NOT safe to upload", total_losses)
        return 1
    logger.info("All files converted with zero data loss.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

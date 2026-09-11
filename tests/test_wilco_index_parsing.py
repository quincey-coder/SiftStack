"""Williamson trustee-sale index PDF — layout parser regression tests.

Fixtures: tests/fixtures/wilco_2026-09_IDX.pdf (75 rows, 2 page groups, merged
"61 WASHGUYS 5 LLC" cell) and wilco_2026-08_IDX.pdf (106 rows; row 6 has the
city typed into the address cell — the case a positional parser silently
shifts). The matching *.txt files are pdfminer text dumps used for expected
values.

Run: PYTHONPATH=src python tests/test_wilco_index_parsing.py   (or pytest)
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import foreclosure_wilco as fw  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
SEPT = (FIX / "wilco_2026-09_IDX.pdf").read_bytes()
AUG = (FIX / "wilco_2026-08_IDX.pdf").read_bytes()


def _owner_block(txt: str) -> list[str]:
    """Owner names from the text dump: lines after OWNER'S LAST NAME until the FILE header."""
    lines = txt.splitlines()
    out, on = [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("OWNER'S LAST NAME"):
            on = True
            continue
        if on and s == "FILE":
            break
        if on and s and re.match(r"^[A-Z]", s):
            out.append(re.sub(r"\s+", " ", s))
    return out


def test_september_rows_and_entry_check():
    rows, meta = fw.extract_index_rows(SEPT)
    assert meta["index_rows"] == 75, meta
    assert meta["index_entry_check"], [r["entry"] for r in rows]
    assert meta["pages"] == 2
    txt = (FIX / "wilco_2026-09_IDX.txt").read_text(encoding="utf-8")
    assert meta["no_address"] == txt.count("NO ADDRESS") == 29, meta
    assert meta["no_city"] == txt.count("NO CITY") == 29, meta
    by = {r["entry"]: r for r in rows}
    assert by[61]["owner"] == "WASHGUYS 5 LLC", by[61]
    assert by[54]["owner"] == "YEARY", by[54]
    # first page owners from the text dump all present
    owners = {r["owner"] for r in rows}
    for o in _owner_block(txt):
        assert o in owners, o
    # date/address split on a merged cell
    koonts = [r for r in rows if "KOONTS" in r["address"]][0]
    assert koonts["created"] == "7/7/2026" and koonts["address"] == "297 KOONTS LOOP", koonts
    assert koonts["city"] == "JARRELL", koonts
    # every row has a creation date
    assert all(re.match(r"\d{1,2}/\d{1,2}/\d{4}$", r["created"]) for r in rows)


def test_august_rows_and_merged_city():
    rows, meta = fw.extract_index_rows(AUG)
    assert meta["index_rows"] == 106, meta
    assert meta["index_entry_check"], [r["entry"] for r in rows]
    bc = [r for r in rows if "1606 BUTTERCUP" in r["address"]]
    assert len(bc) == 1, bc
    assert bc[0]["address"] == "1606 BUTTERCUP CREEK BLVD" and bc[0]["city"] == "CEDAR PARK", bc
    txt = (FIX / "wilco_2026-08_IDX.txt").read_text(encoding="utf-8")
    assert meta["no_address"] == txt.count("NO ADDRESS"), meta
    # NO CITY in the dump + the one city swallowed into the address cell == blank cities
    owners = {r["owner"] for r in rows}
    for o in _owner_block(txt):
        assert o in owners, o


def test_notices_built_from_index():
    notices, meta = fw.parse_index_pdf_bytes(SEPT, "September", 2026)
    assert len(notices) == 75 and meta["index_entry_check"]
    by_addr = {n.address: n for n in notices}
    n = by_addr["297 Koonts Loop"]
    assert n.county == "Williamson" and n.notice_type == "foreclosure"
    assert n.auction_date == "2026-09-01" and n.date_added == "2026-07-07"
    assert n.city == "Jarrell"
    assert n.raw_text.startswith("ENTRY ")
    assert n.source_url.endswith("/September/files.aspx")
    # trustee blocklist keeps working: no owner_name equals a blocklisted trustee
    from scrapers.trustee_blocklist import is_substitute_trustee
    assert not any(is_substitute_trustee(x.owner_name) for x in notices if x.owner_name)
    # papertrail name kept only when an owner survived
    with_owner = [x for x in notices if x.owner_name]
    assert with_owner and all(x.tax_owner_name for x in with_owner)


def test_old_text_parser_is_gone():
    try:
        fw._parse_index_pdf("text", "September", 2026)
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError")


def test_first_tuesday():
    assert fw._first_tuesday("October", 2026) == "2026-10-06"
    assert fw._first_tuesday("September", 2026) == "2026-09-01"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

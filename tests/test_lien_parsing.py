"""Tests for Travis lien parsing + lien formatter wiring.

Grid lines below are REAL tccsearch.org output captured live on 2026-06-29
(see scrapers/lien_travis.py docstring). Run standalone or via pytest:

    PYTHONPATH=src python tests/test_lien_parsing.py
    PYTHONPATH=src pytest tests/test_lien_parsing.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers.lien_travis import _parse_body_text  # noqa: E402
from scrapers.lien_publicsearch import _parse_results_text  # noqa: E402
from scrapers.lien_common import pick_debtor, is_creditor  # noqa: E402
from datasift_formatter import _build_tags, _build_row, NOTICE_TYPE_TO_LIST  # noqa: E402


# Real publicsearch results-table innerText (Bell County, captured live
# 2026-06-29). Columns are tab-separated with empty leading checkbox/image
# cells: GRANTOR  GRANTEE  DOC TYPE  RECORDED DATE  INST  BOOK  PROP DESC.
PUBLICSEARCH_TABLE = "\n".join([
    "Search Results",
    "Document Types",
    "Search results table for Property Records",
    "\t\t\tGRANTOR\tGRANTEE\tDOC TYPE\tRECORDED DATE\tINST NUMBER\tBOOK/VOLUME/PAGE\tPROPERTY DESCRIPTION",
    "\t\t\tMONTEITH ABSTRACT CO\tHALE DAVID ALLEN\tABSTRACT OF JUDGMENT\t12/14/1967\t1967001353\tOPR/13/33\tProperty Description: $116.25",
    "\t\t\tLAND EXCHANGE ABSTRACT & TITLE COMPANY\tFUHRMAN JOSEPH A\tABSTRACT OF JUDGMENT\t11/26/2007\t2007049912\tOPR/6656/68\tProperty Description: $8000.00 + COSTS",
    "\t\t\tSTATE OF TEXAS\tACME WIDGETS LLC\tFEDERAL TAX LIEN\t6/2/2026\t2026012345\tOPR/9001/12\tProperty Description: $42,000.00",
])


# Real grid (header noise + 3 live records) plus one synthetic mechanics-lien
# row that carries a property legal description (LOC) to exercise address parse.
SAMPLE_GRID = "\n".join([
    "Travis County, Texas",
    "County Clerk Web Search",
    "Showing Records 1 through 20 ( 300 records found )",
    "#\tImage\t\tInstrument #",
    "1\t\t\t2026076222",
    "06/29/2026\tABSTRACT OF JUDGMENT\t[R] CITIBANK",
    "[E] MOLINA RAMON G",
    "Temp",
    "2\t\t\t2026076216",
    "06/29/2026\tABSTRACT OF JUDGMENT\t[R] MIDLAND CREDIT MANAGEMENT INC (+)",
    "[E] THOMAS CLARENCE",
    "Temp",
    "3\t\t\t2026076200",
    "06/29/2026\tSTATE TAX LIEN\t[R] STATE OF TEXAS",
    "[E] WORKDAY INC",
    "Temp",
    "4\t\t\t2026076100",
    "06/15/2026\tMECHANICS LIEN\t[R] ABC CONSTRUCTION LLC",
    "[E] SMITH JOHN",
    "LOC 123 MAIN ST AUSTIN TX 78701",
    "Temp",
])


def test_parses_all_records():
    notices = _parse_body_text(SAMPLE_GRID)
    assert len(notices) == 4, f"expected 4 liens, got {len(notices)}"


def test_lead_is_debtor_not_creditor():
    """The lead must be the [E] debtor, never the [R] creditor."""
    n = _parse_body_text(SAMPLE_GRID)[0]
    assert n.tax_owner_name == "MOLINA RAMON G"
    assert n.owner_name and "citibank" not in n.owner_name.lower()
    assert n.lien_creditor.lower() == "citibank"
    assert n.lien_type == "Abstract Of Judgment"
    assert n.notice_type == "lien"
    assert n.county == "Travis"
    assert n.date_added == "2026-06-29"
    assert "2026076222" in n.source_url
    # AJ records are name-indexed — no address until CAD lookup fills it
    assert n.address == ""


def test_state_tax_lien_business_debtor():
    n = _parse_body_text(SAMPLE_GRID)[2]
    assert n.lien_type == "State Tax Lien"
    assert n.tax_owner_name == "WORKDAY INC"
    assert n.lien_creditor.lower().startswith("state of texas")


def test_mechanics_lien_with_address():
    n = _parse_body_text(SAMPLE_GRID)[3]
    assert n.lien_type == "Mechanics Lien"
    assert n.tax_owner_name == "SMITH JOHN"
    assert n.address == "123 Main St"
    assert n.city == "Austin"
    assert n.zip == "78701"


def test_publicsearch_parses_table_rows():
    notices = _parse_results_text(PUBLICSEARCH_TABLE, "Bell")
    assert len(notices) == 3, f"expected 3, got {len(notices)}"


def test_publicsearch_grantee_is_lead():
    """GRANTEE (debtor) is the lead; GRANTOR (filer) is creditor context."""
    n = _parse_results_text(PUBLICSEARCH_TABLE, "Bell")[0]
    assert n.tax_owner_name == "HALE DAVID ALLEN"
    assert n.lien_creditor.lower().startswith("monteith")
    assert n.lien_type == "Abstract Of Judgment"
    assert n.notice_type == "lien"
    assert n.county == "Bell"
    assert n.date_added == "1967-12-14"
    assert "2007049912" not in n.source_url  # row 0 instrument is 1967001353
    assert "1967001353" in n.source_url


def test_publicsearch_reversed_party_roles():
    """Live Bell data: the GRANTEE is the creditor (Midland) and the GRANTOR is
    the individual debtor — pick_debtor must still return the individual."""
    table = "\n".join([
        "Search results table for Property Records",
        "\t\t\tGRANTOR\tGRANTEE\tDOC TYPE\tRECORDED DATE\tINST NUMBER\tBOOK\tPROP",
        "\t\t\tVOWIELL RYAN\tMIDLAND CREDIT MANAGEMENT INC\tABSTRACT OF JUDGMENT\t6/1/2026\t2026027004\tOPR/1/2\t$",
    ])
    n = _parse_results_text(table, "Bell")[0]
    assert n.tax_owner_name == "VOWIELL RYAN", n.tax_owner_name
    assert n.lien_creditor.lower().startswith("midland"), n.lien_creditor


def test_publicsearch_federal_tax_lien_business_debtor():
    n = _parse_results_text(PUBLICSEARCH_TABLE, "Bell")[2]
    assert n.lien_type == "Federal Tax Lien"
    assert n.tax_owner_name == "ACME WIDGETS LLC"
    assert n.date_added == "2026-06-02"
    assert n.lien_creditor.lower().startswith("state of texas")


def test_pick_debtor_institutional_creditors():
    """Govt, debt-buyers, banks, hospitals, insurers are creditors — the lead is
    always the OTHER (individual) party, whichever column it sits in."""
    # IRS on the grantee side (Federal Tax Lien)
    assert pick_debtor("CHAVARRIA JASEON", "IRS")[0] == "CHAVARRIA JASEON"
    # Hospital on the grantee side (Hospital Lien)
    assert pick_debtor("PEGUES ANITA", "ADVENTHEALTH CENTRAL TEXAS")[0] == "PEGUES ANITA"
    # Debt buyer on the grantee side (Abstract of Judgment)
    assert pick_debtor("VOWIELL RYAN", "MIDLAND CREDIT MANAGEMENT INC")[0] == "VOWIELL RYAN"
    # Creditor on the grantor side (Travis-style: [R] CITIBANK / [E] MOLINA)
    assert pick_debtor("CITIBANK", "MOLINA RAMON G")[0] == "MOLINA RAMON G"
    # State tax authority
    assert pick_debtor("STATE OF TEXAS", "WORKDAY INC")[0] == "WORKDAY INC"
    # Two individuals → default grantee = debtor
    assert pick_debtor("SMITH JANE", "DOE JOHN")[0] == "DOE JOHN"
    assert is_creditor("IRS") and is_creditor("BAYLOR SCOTT & WHITE")
    assert not is_creditor("MOLINA RAMON G")


def test_formatter_lien_wiring():
    n = _parse_body_text(SAMPLE_GRID)[0]
    n.zip = "78701"  # pretend CAD lookup filled the property location
    tags = _build_tags(n)
    assert "lien" in tags.split(",")
    assert "abstract_of_judgment" in tags
    assert "Courthouse Data" in tags
    row = _build_row(n)
    assert row["Lists"] == "Lien"
    assert NOTICE_TYPE_TO_LIST["lien"] == "Lien"
    # Lien context surfaced in Notes
    assert "Abstract Of Judgment" in row["Notes"]
    assert "Citibank" in row["Notes"]


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
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

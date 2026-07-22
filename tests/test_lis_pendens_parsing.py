"""Tests for lis pendens parsing + formatter wiring (Tex. Prop. Code § 12.007).

Grid/table shapes mirror the live tccsearch.org (Travis) and publicsearch.us
(Bell) formats proven by the lien scrapers. Run standalone or via pytest:

    PYTHONPATH=src python tests/test_lis_pendens_parsing.py
    PYTHONPATH=src pytest tests/test_lis_pendens_parsing.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers.lis_pendens_travis import _parse_body_text  # noqa: E402
from scrapers.lis_pendens_publicsearch import _parse_results_text  # noqa: E402
from scrapers.lis_pendens_common import pick_defendant, is_plaintiff  # noqa: E402
from datasift_formatter import _build_tags, _build_row, NOTICE_TYPE_TO_LIST  # noqa: E402


# tccsearch.org Travis grid: [R] = plaintiff/filer, [E] = defendant (our lead).
#   1. HOA foreclosure LP with a LOC legal description (address parses inline)
#   2. Divorce LP — two individuals; default grantee = defendant
#   3. A RELEASE of lis pendens — must be DROPPED (stale, not a live distress)
SAMPLE_GRID = "\n".join([
    "Travis County, Texas",
    "County Clerk Web Search",
    "Showing Records 1 through 20 ( 137 records found )",
    "#\tImage\t\tInstrument #",
    "1\t\t\t2026086312",
    "06/29/2026\tLIS PENDENS\t[R] OAK RUN OWNERS ASSOCIATION INC",
    "[E] MOLINA RAMON G",
    "LOC 123 MAIN ST AUSTIN TX 78701",
    "Temp",
    "2\t\t\t2026086300",
    "06/28/2026\tLIS PENDENS\t[R] SMITH JANE",
    "[E] SMITH JOHN",
    "Temp",
    "3\t\t\t2026086111",
    "06/20/2026\tRELEASE OF LIS PENDENS\t[R] OAK RUN OWNERS ASSOCIATION INC",
    "[E] DOE JOHN",
    "Temp",
])


# publicsearch.us (Bell) tab-separated results table (empty leading cells).
# LP carries a subdivision legal, not a mailing address → address stays blank
# (backfilled by CAD name lookup in enrichment Step 3c).
PUBLICSEARCH_TABLE = "\n".join([
    "Search Results",
    "Search results table for Property Records",
    "\t\t\tGRANTOR\tGRANTEE\tDOC TYPE\tRECORDED DATE\tINST NUMBER\tBOOK/VOLUME/PAGE\tPROPERTY DESCRIPTION",
    "\t\t\tSTONE CANYON HOMEOWNERS ASSOCIATION\tHALE DAVID ALLEN\tLIS PENDENS\t6/14/2026\t2026001353\tOPR/13/33\tLOT 4 BLK B STONE CANYON",
    "\t\t\tGARCIA MARIA\tGARCIA LUIS A\tNOTICE OF LIS PENDENS\t5/2/2026\t2026012345\tOPR/90/12\tLOT 9 SUNSET RIDGE",
])


# ── Travis grid ──────────────────────────────────────────────────────────

def test_travis_parses_live_records_drops_release():
    notices = _parse_body_text(SAMPLE_GRID)
    # 3 rows in the grid, but the RELEASE row is dropped → 2 live lis pendens.
    assert len(notices) == 2, f"expected 2, got {len(notices)}"


def test_travis_lead_is_defendant_not_plaintiff():
    """Lead = the [E] defendant/owner; the [R] HOA plaintiff is context only."""
    n = _parse_body_text(SAMPLE_GRID)[0]
    assert n.tax_owner_name == "MOLINA RAMON G"
    assert n.owner_name and "association" not in n.owner_name.lower()
    assert n.lien_creditor.lower().startswith("oak run")   # plaintiff surfaced
    assert n.notice_type == "lis_pendens"
    assert n.county == "Travis"
    assert n.date_added == "2026-06-29"
    assert "2026086312" in n.source_url


def test_travis_address_parsed_from_legal():
    n = _parse_body_text(SAMPLE_GRID)[0]
    assert n.address == "123 Main St"
    assert n.city == "Austin"
    assert n.zip == "78701"


def test_travis_two_individuals_default_grantee():
    """Divorce/partition LP — neither party institutional → grantee is the lead."""
    n = _parse_body_text(SAMPLE_GRID)[1]
    assert n.tax_owner_name == "SMITH JOHN"
    assert n.address == ""   # no LOC → filled later by CAD name lookup


# ── Bell publicsearch table ──────────────────────────────────────────────

def test_publicsearch_parses_both_spellings():
    notices = _parse_results_text(PUBLICSEARCH_TABLE, "Bell")
    assert len(notices) == 2, f"expected 2, got {len(notices)}"


def test_publicsearch_defendant_is_lead():
    n = _parse_results_text(PUBLICSEARCH_TABLE, "Bell")[0]
    assert n.tax_owner_name == "HALE DAVID ALLEN"
    assert n.lien_creditor.lower().startswith("stone canyon")
    assert n.notice_type == "lis_pendens"
    assert n.county == "Bell"
    assert n.date_added == "2026-06-14"
    assert "2026001353" in n.source_url
    assert n.address == ""   # subdivision legal, not a mailing address


# ── Party-picker unit tests ──────────────────────────────────────────────

def test_pick_defendant_and_is_plaintiff():
    # HOA plaintiff on the grantor side → grantee is the defendant
    assert pick_defendant("OAK RUN OWNERS ASSOCIATION INC", "MOLINA RAMON G")[0] == "MOLINA RAMON G"
    # Lender plaintiff (bank keyword via lien_common.is_creditor)
    assert pick_defendant("WELLS FARGO BANK NA", "DOE JOHN")[0] == "DOE JOHN"
    # Law firm filing on grantor side
    assert pick_defendant("HUGHES WATTERS ASKANASE LLP", "NGUYEN TAM")[0] == "NGUYEN TAM"
    # Two individuals (divorce) → default grantee = defendant
    assert pick_defendant("SMITH JANE", "SMITH JOHN")[0] == "SMITH JOHN"
    assert is_plaintiff("STONE CANYON HOMEOWNERS ASSOCIATION")
    assert is_plaintiff("IRS")          # institutional creditor also = plaintiff
    assert not is_plaintiff("MOLINA RAMON G")


def test_pick_defendant_taxing_unit_reversed_index():
    """Live Bell 2026-07-22: batch tax-suit LPs indexed the TAXING DISTRICT as
    GRANTEE and the taxpayer as GRANTOR — the taxpayer must be the lead, and
    the clerk's dangling 'Aka' marker must be stripped."""
    d, p = pick_defendant("WALKER VERDIE AKA", "TAX APPRAISAL DISTRICT OF BELL COUNTY")
    assert d == "WALKER VERDIE", f"lead={d!r}"
    assert p == "TAX APPRAISAL DISTRICT OF BELL COUNTY"
    # City plaintiff (clerk-inverted "GEORGETOWN CITY OF" form)
    assert pick_defendant("GEORGETOWN CITY OF", "SUPAK BARBARA")[0] == "SUPAK BARBARA"
    # Tax-collection law firm filing for a taxing unit
    assert pick_defendant("LINEBARGER GOGGAN BLAIR & SAMPSON LLP", "DOE JOHN")[0] == "DOE JOHN"
    assert is_plaintiff("TAX APPRAISAL DISTRICT OF BELL COUNTY")
    assert is_plaintiff("ROUND ROCK ISD")
    # A plain person named e.g. "Cityof" edge shouldn't trip — sanity negatives
    assert not is_plaintiff("WALKER VERDIE")
    assert not is_plaintiff("SUPAK BARBARA")


def test_pick_defendant_clerk_placeholder_never_lead():
    """Live Travis 2026-07-13: probate LP indexed as [R] PARADA MICHAEL P DECD /
    [E] GRANTEE UNKNOWN — the placeholder must never become the lead."""
    defendant, _ = pick_defendant("PARADA MICHAEL P DECD", "GRANTEE UNKNOWN")
    assert defendant == "", f"placeholder leaked as lead: {defendant!r}"
    # And a placeholder on the grantor side doesn't block a real grantee lead
    assert pick_defendant("UNKNOWN", "DOE JOHN")[0] == "DOE JOHN"


# ── Formatter wiring ─────────────────────────────────────────────────────

def test_formatter_lis_pendens_wiring():
    n = _parse_body_text(SAMPLE_GRID)[0]   # HOA LP, address already parsed
    tags = _build_tags(n)
    assert "Lis Pendens" in tags.split(",")
    assert "Courthouse Data" in tags
    assert "travis" in tags.split(",")
    row = _build_row(n)
    assert row["Lists"] == "Lis Pendens"
    assert NOTICE_TYPE_TO_LIST["lis_pendens"] == "Lis Pendens"
    # Notes label the filer as the PLAINTIFF (not "Creditor"), and show the type.
    assert "Plaintiff: Oak Run Owners Association Inc" in row["Notes"]
    assert "Lis Pendens" in row["Notes"]
    assert "Creditor:" not in row["Notes"]


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

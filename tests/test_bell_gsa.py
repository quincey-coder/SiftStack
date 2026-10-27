"""Offline parse tests for the Bell CAD GSA portal reader (src/bell_gsa.py).

Fixtures are the live pages for parcel 0000100046 captured 2026-10-27
(tests/fixtures/bell_gsa/). No network.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bell_gsa  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "bell_gsa"


@pytest.fixture(scope="module")
def parcel_page():
    return (FIX / "parcel_0000100046.html").read_text(encoding="utf-8", errors="ignore")


@pytest.fixture(scope="module")
def tax_page():
    return (FIX / "tax_0000100046.html").read_text(encoding="utf-8", errors="ignore")


def test_urls_zero_pad_ids():
    assert bell_gsa.pad_id("100046") == "0000100046"
    assert bell_gsa.pad_id(100046) == "0000100046"
    assert bell_gsa.pad_id("R-0000100046") == "0000100046"
    assert bell_gsa.pad_id(100046.0) == "0000100046"       # openpyxl numeric cell
    assert bell_gsa.pad_id("100046.0") == "0000100046"
    assert bell_gsa.pad_id(None) == "" and bell_gsa.pad_id("") == ""
    assert bell_gsa.parcel_url("100046").endswith("/parcel/0000100046")
    assert bell_gsa.tax_url(100046).endswith("/tax/0000100046")


def test_money_keeps_sign_on_credits():
    assert bell_gsa._money("$1,028.21") == 1028.21
    assert bell_gsa._money("-$12.40") == -12.40
    assert bell_gsa._money("$-9.14") == -9.14
    assert bell_gsa._money("($5.00)") == -5.00
    assert bell_gsa._money("Pay $709.58") == 709.58
    assert bell_gsa._money("N/A") is None


def test_parse_parcel_owner_situs_and_deeds(parcel_page):
    d = bell_gsa.parse_parcel(parcel_page)
    assert d["prop_id"] == "0000100046"
    assert d["owner_name"] == "GOMEZ, JOSE"
    assert d["mailing_street"] == "1710 SHERMAN DR"
    assert d["mailing_city_state_zip"] == "KILLEEN, TX 76543-3247"
    assert d["situs"] == "1508 N GRAY ST"
    assert d["use_code"].startswith("A1")
    assert d["subdivision"] == "NORTH SIDE ADDITION"
    assert d["as_of"] == "October 27, 2026"

    deeds = d["deeds"]
    assert [x["type"] for x in deeds] == ["WARRANTY DEED", "AFFIDAVIT OF HEIRSHIP", "WARRANTY DEED"]
    assert deeds[0]["instrument"] == "2025023358"
    assert deeds[0]["date"] == "2025-01-13"
    assert deeds[0]["grantee"] == "GOMEZ, JOSE"
    assert deeds[0]["grantor"].startswith("RAMIREZ, GUSTAVO ADOLFO")
    assert deeds[1]["grantor"] == "NORIEGA, TRINIDAD ROCHA & GUSTAVO RAMIREZ"
    assert deeds[2]["instrument"] == "3588/36"  # volume/page era


def test_parse_parcel_buildings_and_values(parcel_page):
    d = bell_gsa.parse_parcel(parcel_page)
    assert d["buildings"][0] == {
        "type": "RESIDENTIAL", "description": "LOW QUAL STORAGE",
        "year_built": "1958", "sqft": "224",
    }
    vh = d["value_history"]
    assert 2026 in vh and 2021 in vh
    assert vh[2026]["Market"] == "$25,155"
    assert vh[2026]["Taxable"] == "$25,155"
    assert vh[2027]["Market"] == "N/A"


def test_parse_tax_balances_and_years(tax_page):
    d = bell_gsa.parse_tax(tax_page, today=date(2026, 10, 27))
    assert d["prop_id"] == "0000100046"
    assert d["owner_name"] == "GOMEZ, JOSE"
    assert d["situs"].startswith("1508 N GRAY ST")
    assert d["balance"] == 1028.21
    assert d["currently_due"] == 1028.21
    assert d["current_year"] == 2026
    assert d["as_of"] == "10/27/2026"

    by_year = {r["year"]: r for r in d["years"]}
    assert by_year[2026]["current"] is True and by_year[2026]["balance"] == 518.27
    assert by_year[2026]["delinquent"] is False and by_year[2026]["due_date"] == "2027-01-31"
    assert by_year[2026]["interest"] == 0.0 and by_year[2026]["penalty"] is None
    assert by_year[2025] == {
        "year": 2025, "original": 509.94, "penalty": 0.0, "interest": 199.64,
        "paid": 0.0, "balance": 709.58, "current": False,
        "due_date": "2026-01-31", "delinquent": True,
    }
    assert by_year[2024]["paid"] == 500.42 and by_year[2024]["balance"] == 0.0
    assert min(by_year) == 2005 and len(by_year) == 22

    # 2025 is the only prior year still owed; 2026 is a current bill, not delinquent.
    assert d["delinquent_years"] == 1
    assert d["delinquent_balance"] == 709.58
    assert d["oldest_delinquent_year"] == 2025
    assert d["last_paid_year"] == 2024


def test_current_year_bill_becomes_delinquent_after_jan_31(tax_page):
    """Same page read on 2027-05-01: the unpaid 2026 bill is now delinquent too."""
    d = bell_gsa.parse_tax(tax_page, today=date(2027, 5, 1))
    assert d["delinquent_years"] == 2
    assert d["delinquent_balance"] == round(709.58 + 518.27, 2)
    assert d["oldest_delinquent_year"] == 2025
    # ...and on the due date itself it is still current.
    d = bell_gsa.parse_tax(tax_page, today=date(2027, 1, 31))
    assert d["delinquent_years"] == 1


def test_unknown_parcel_raises_not_silent_zero(parcel_page, tax_page):
    """/parcel/<bogus> is an empty 200, /tax/<bogus> a generic page echoing the id."""
    with pytest.raises(bell_gsa.BellParcelNotFound):
        bell_gsa._assert_found(bell_gsa.parse_parcel(""), "parcel", "9999999999")
    generic_tax = "<h1>Tax Account 9999999999</h1><h3>Account Summary</h3>"
    with pytest.raises(bell_gsa.BellParcelNotFound):
        bell_gsa._assert_found(bell_gsa.parse_tax(generic_tax), "tax", "9999999999")
    # real pages pass
    bell_gsa._assert_found(bell_gsa.parse_parcel(parcel_page), "parcel", "100046")
    bell_gsa._assert_found(bell_gsa.parse_tax(tax_page), "tax", "100046")


def test_parse_livesearch_tolerates_tr_attributes():
    payload = json.dumps({"html": (
        '<table><tr class="r"><td><a href="/parcel/0000000001">1</a></td>'
        '<td>Real Property<span>&nbsp;&middot;&nbsp;<a href="/tax/0000000001">Taxes</a></span></td>'
        '<td>DOE, JANE</td><td>1 MAIN ST</td></tr></table>')})
    assert bell_gsa.parse_livesearch(payload)[0]["prop_id"] == "0000000001"


def test_parse_livesearch_dedupes_record_types():
    rows = bell_gsa.parse_livesearch((FIX / "livesearch_100046.json").read_text(encoding="utf-8"))
    assert rows == [{
        "prop_id": "0000100046", "kind": "Real Property", "has_tax": True,
        "name": "GOMEZ, JOSE", "address": "1508 N GRAY ST",
    }]

"""Tests for parcel→owner backfill: parcel key normalization, ambiguity guard,
and the tax-owner name-reversal fix + entity fallback in the formatter."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travis_tax_cache import _normalize_parcel, _add_parcel
from datasift_formatter import _get_contact_info
from notice_parser import NoticeData


# ── Parcel key normalization ────────────────────────────────────────

def test_normalize_austin_10_and_tcad_14_align():
    # Austin's 10-digit parcelid and TCAD's 14-digit PARCEL reduce to the same key
    assert _normalize_parcel("0256301015") == "0256301015"
    assert _normalize_parcel("02563010150000") == "0256301015"
    assert _normalize_parcel("0256301015") == _normalize_parcel("02563010150000")


def test_normalize_parcel_short_or_empty():
    assert _normalize_parcel("") == ""
    assert _normalize_parcel("123") == ""          # fewer than 10 digits → no key
    assert _normalize_parcel(None) == ""


# ── Ambiguity guard ─────────────────────────────────────────────────

def _rec(owner, source="current_mailing", parcel="02563010150000"):
    return {"fullname": owner, "quickrefid": parcel, "source": source}


def test_parcel_single_owner_kept():
    idx = {}
    _add_parcel(idx, _rec("GO GREEN LIVING LLC"))
    assert idx["0256301015"]["fullname"] == "GO GREEN LIVING LLC"


def test_parcel_same_surname_variants_not_ambiguous():
    idx = {}
    _add_parcel(idx, _rec("MCDONALD ROBERT J & MEGAN L"))
    _add_parcel(idx, _rec("MCDONALD ROBERT J", parcel="02563010150001"))
    assert isinstance(idx["0256301015"], dict)  # same surname → not ambiguous


def test_parcel_two_owners_ambiguous():
    idx = {}
    _add_parcel(idx, _rec("SMITH JOHN"))
    _add_parcel(idx, _rec("JONES MARY", parcel="02563010150001"))
    assert idx["0256301015"] == "__AMBIG__"  # condo/apartment → declined


def test_parcel_delinquent_authoritative():
    idx = {}
    _add_parcel(idx, _rec("SMITH JOHN"))
    _add_parcel(idx, _rec("JONES MARY", parcel="02563010150001"))  # now ambiguous
    _add_parcel(idx, _rec("REALOWNER LLC", source="delinquent_situs"))
    assert idx["0256301015"]["fullname"] == "REALOWNER LLC"  # authoritative resolves


# ── Formatter: tax LAST-FIRST name fix + entity fallback ────────────

def _cv(tax_owner):
    n = NoticeData(notice_type="code_violation", county="Travis",
                   address="1 Main St", city="Austin", state="TX", zip="78702")
    n.tax_owner_name = tax_owner
    return n


def test_tax_owner_name_not_reversed():
    # Tax roll is LAST FIRST; must not come out reversed
    c = _get_contact_info(_cv("JEFFEIS TANISA"))
    assert c["first"] == "Tanisa" and c["last"] == "Jeffeis"


def test_tax_owner_middle_initial_not_lost():
    c = _get_contact_info(_cv("MCDONALD ROBERT J & MEGAN L"))
    assert c["first"] == "Robert" and c["last"] == "Mcdonald"  # not Last='J'


def test_entity_owner_filled_in_business():
    # Entity owners now route to Business Name (not Last Name).
    c = _get_contact_info(_cv("GO GREEN LIVING LLC"))
    assert c["first"] == "" and c["last"] == "" and "Go Green Living" in c["business"]


def test_no_tax_owner_stays_blank():
    n = NoticeData(notice_type="code_violation", county="Travis",
                   address="1 Main St", city="Austin", state="TX", zip="78702")
    c = _get_contact_info(n)
    assert c["first"] == "" and c["last"] == "" and c["business"] == ""


# ── Business Name routing ───────────────────────────────────────────

def test_entity_routes_to_business_not_last():
    c = _get_contact_info(_cv("SOUTHPARK JV LLC"))
    assert "Southpark Jv" in c["business"] and c["first"] == "" and c["last"] == ""


def test_individual_no_business():
    c = _get_contact_info(_cv("WILHITE MADLYN L"))
    assert c["business"] == "" and c["first"] == "Madlyn" and c["last"] == "Wilhite"


def test_owner_name_entity_with_agent_gets_both():
    n = NoticeData(notice_type="foreclosure", county="Travis",
                   address="1 Main St", city="Austin", state="TX", zip="78702")
    n.owner_name = "NESTOR SOLUTIONS LLC"
    n.entity_person_name = "Jane Doe"
    c = _get_contact_info(n)
    assert "Nestor Solutions" in c["business"] and c["first"] == "Jane" and c["last"] == "Doe"


def test_mailing_always_populated_falls_back_to_property():
    n = NoticeData(notice_type="tax_sale", county="Travis",
                   address="100 Main St", city="Austin", state="TX", zip="78702")
    n.owner_name = "Cora Berenguer"
    c = _get_contact_info(n)
    assert c["street"] == "100 Main St" and c["city"] == "Austin"  # mailing = property


def test_validate_row_business_satisfies_owner():
    from datasift_formatter import _validate_row
    row = {"Business Name": "Acme Holdings Inc", "Owner First Name": "",
           "Owner Last Name": "", "Property Street Address": "1 Main St",
           "Mailing Street Address": "1 Main St"}
    complete, issues = _validate_row(row)
    assert complete and not issues  # business name → not flagged incomplete


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); passed += 1; print(f"  PASS  {name}")
            except Exception as e:
                failed += 1; print(f"  FAIL  {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

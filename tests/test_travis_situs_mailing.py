"""The owner's MAILING address must never be promoted to the PROPERTY (situs)
address — the 2026-08-27/29 lien lists shipped an LLC's PO Box and a debtor's
office suite as property addresses because the Travis current-roll cache
stored the mailing address in its `situsaddress` slot.

Offline: the tax cache and the TCAD GIS resolver are monkeypatched.

Run:  PYTHONPATH=src python tests/test_travis_situs_mailing.py
      PYTHONPATH=src pytest tests/test_travis_situs_mailing.py
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))  # noqa: E402

import cad_lookup  # noqa: E402
import travis_gis  # noqa: E402
import travis_tax_cache  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


def _cur(owner, mailing, city="AUSTIN", state="TX", zipc="78704", parcel="02472611610000"):
    """A record in the shape `_load_cur` now emits (blank situs, explicit mailing)."""
    return {"fullname": owner, "situsaddress": "", "scity": "", "sstate": "TX", "szip": "",
            "mailing": mailing, "mcity": city, "mstate": state, "mzip": zipc,
            "quickrefid": parcel, "totalpropmktvalue": "", "propertytypedesc": "",
            "delinquent_total": "", "years_delinquent": "", "source": "current_mailing"}


def _delq(owner, situs, zipc="78753", parcel="02472611610000", mailing="PO BOX 1", mcity="HOUSTON"):
    return {"fullname": owner, "situsaddress": situs, "scity": "AUSTIN", "sstate": "TX", "szip": zipc,
            "mailing": mailing, "mcity": mcity, "mstate": "TX", "mzip": "77002",
            "quickrefid": parcel, "totalpropmktvalue": "250000", "propertytypedesc": "A1",
            "delinquent_total": "1200.00", "years_delinquent": "3", "source": "delinquent_situs"}


class _Patched:
    """Route cad_lookup's Travis name search + GIS resolver to canned data."""

    def __init__(self, records, gis=None):
        self.records, self.gis = records, gis or {}

    def __enter__(self):
        self._sbn = travis_tax_cache.search_by_name
        self._gis = travis_gis.situs_by_geo_id
        travis_tax_cache.search_by_name = lambda last, first="": list(self.records)
        travis_gis.situs_by_geo_id = lambda pid: self.gis.get(travis_gis.normalize_geo_id(pid))
        travis_gis._memo.clear()
        return self

    def __exit__(self, *a):
        travis_tax_cache.search_by_name = self._sbn
        travis_gis.situs_by_geo_id = self._gis


GIS_HIT = {"0247261161": {"geo_id": "0247261161", "prop_id": "980439", "address": "11311 Wedgewood Dr",
                          "city": "Austin", "state": "TX", "zip": "78753", "source": "tcad_gis"}}


# ── lookup_property_by_name ─────────────────────────────────────────────

def test_lien_default_never_uses_mailing_po_box():
    """The exact 2026-08-29 case: LLC with a PO Box mailing, GIS knows the parcel."""
    with _Patched([_cur("ATLANTIC URBANA II BRAKER LANE LLC", "P.O. BOX 41539")], GIS_HIT):
        r = cad_lookup.lookup_property_by_name("ATLANTIC URBANA II BRAKER LANE LLC", "Travis")
    assert r is not None
    assert r["address"] == "11311 Wedgewood Dr" and r["zip"] == "78753"
    assert r["address_basis"] == "gis_situs" and r["source"] == "current_mailing"
    assert r["mail_street"] == "P.O. Box 41539" and r["mail_zip"] == "78704"


def test_lien_default_returns_none_when_no_situs():
    """No GIS situs and no opt-in → no property address (never the mailing)."""
    with _Patched([_cur("RYAN JOHN", "500 W 2ND ST STE 1900", zipc="78701", parcel="04040733020000")]):
        r = cad_lookup.lookup_property_by_name("RYAN JOHN", "Travis")
    assert r is None


def test_probate_opt_in_uses_residential_mailing_and_labels_it():
    with _Patched([_cur("SMITH JANE", "1600 HOLLY ST", zipc="78702")]):
        r = cad_lookup.lookup_property_by_name("SMITH JANE", "Travis", allow_mailing_as_situs=True)
    assert r and r["address"] == "1600 Holly St" and r["zip"] == "78702" and r["city"] == "Austin"
    assert r["address_basis"] == "mailing_heuristic"


def test_probate_opt_in_prefers_gis_situs_over_mailing():
    with _Patched([_cur("SMITH JANE", "1600 HOLLY ST", zipc="78702")], GIS_HIT):
        r = cad_lookup.lookup_property_by_name("SMITH JANE", "Travis", allow_mailing_as_situs=True)
    assert r["address"] == "11311 Wedgewood Dr" and r["address_basis"] == "gis_situs"


def test_probate_opt_in_still_rejects_po_box_and_suite_and_out_of_state():
    for mailing, kw in (("P.O. BOX 41539", {}), ("500 W 2ND ST STE 1900", {}),
                        ("100 MAIN ST", {"state": "OK"})):
        with _Patched([_cur("SMITH JANE", mailing, **kw)]):
            r = cad_lookup.lookup_property_by_name("SMITH JANE", "Travis", allow_mailing_as_situs=True)
        assert r is None, mailing


def test_delinquent_situs_wins_tie_and_keeps_mailing_separate():
    # Two different parcels owned by the same name (the lookup dedupes on
    # parcel id, so a shared id would hide the second record, not rank it).
    recs = [_cur("DOE JOHN", "9 RENTAL OWNER HOME DR", parcel="01112223330000"),
            _delq("DOE JOHN", "4405 AVENUE H", parcel="02472611610000")]
    with _Patched(recs, GIS_HIT):
        r = cad_lookup.lookup_property_by_name("DOE JOHN", "Travis")
    assert r["address"] == "4405 Avenue H" and r["address_basis"] == "situs"
    assert r["source"] == "delinquent_situs"
    assert r["mail_street"] == "Po Box 1" and r["mail_city"] == "Houston"


# ── the cache loader itself ─────────────────────────────────────────────

def test_load_cur_leaves_situs_blank_and_keeps_mailing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "TaxCurOpenData.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["PARCEL", "NAMELF", "MAILINGADDRESS", "ADDRESS1", "CITY", "STATE", "ZIPCODE"])
            w.writerow(["02472611610000", "ATLANTIC URBANA II BRAKER LANE LLC", "P.O. BOX 41539", "", "AUSTIN", "TX", "787040000"])
        idx, addr_idx, parcel_idx = {}, {}, {}
        n = travis_tax_cache._load_cur(path, idx, addr_idx, parcel_idx)
    assert n == 1
    rec = idx["ATLANTIC"][0] if "ATLANTIC" in idx else next(iter(idx.values()))[0]
    assert rec["situsaddress"] == "" and rec["szip"] == ""
    assert rec["mailing"] == "P.O. BOX 41539" and rec["mzip"] == "78704" and rec["mstate"] == "TX"
    assert rec["source"] == "current_mailing"


# ── parcel lookup: owner mailing never falls back to the property address ──

def test_parcel_lookup_mailing_from_mailing_keys_only():
    orig = travis_tax_cache.search_by_parcel
    try:
        travis_tax_cache.search_by_parcel = lambda pid: _delq("DOE JOHN", "4405 AVENUE H")
        r = cad_lookup._parcel_lookup_one("02472611610000", "Travis")
    finally:
        travis_tax_cache.search_by_parcel = orig
    assert r["mail_street"] == "PO BOX 1" and r["mail_city"] == "HOUSTON"


# ── tax-sale backfill uses the parcel fabric, never the mailing ──────────

def test_tax_sale_backfill_uses_gis_for_current_roll_parcel():
    from scrapers import tax_sale_travis
    n = NoticeData(notice_type="tax_sale", county="Travis", state="TX")
    n.parcel_id = "02472611610000"
    n.address = "11311 WEDGEWO"          # RealAuction's truncated preview
    with _Patched([], GIS_HIT):
        orig = travis_tax_cache.search_by_parcel
        try:
            travis_tax_cache.search_by_parcel = lambda pid: _cur("ATLANTIC URBANA II BRAKER LANE LLC", "P.O. BOX 41539")
            tax_sale_travis._backfill_situs([n])
        finally:
            travis_tax_cache.search_by_parcel = orig
    assert n.address == "11311 Wedgewood Dr" and n.zip == "78753" and n.city == "Austin"


# ── GIS feature parsing ─────────────────────────────────────────────────

def test_gis_feature_parsing_strips_zip_and_rejects_unaddressed():
    ok = travis_gis._situs_from_feature({"geo_id": "0226310302", "PROP_ID": 227064, "situs_num": "9500",
                                         "situs_street": "U S HY 290", "situs_zip": "78724",
                                         "situs_address": "9500 E U S HY 290 78724", "situs_city": None})
    assert ok["address"] == "9500 E U S Hy 290" and ok["zip"] == "78724"
    assert travis_gis._situs_from_feature({"geo_id": "1", "situs_num": None, "situs_address": ""}) is None
    assert travis_gis.normalize_geo_id("02472611610000") == "0247261161"
    assert travis_gis.normalize_geo_id("abc") == ""


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  FAIL {name}: {e!r}")
    print("FAILED" if failed else "ALL PASSED", failed)
    sys.exit(1 if failed else 0)

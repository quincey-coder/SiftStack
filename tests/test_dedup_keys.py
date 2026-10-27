"""Dedup identity keys — every courthouse feed keys on its clerk instrument /
court case id, never on the debtor's name or a type-agnostic parcel.

Background (2026-10-22 audit of the production seen-set, 7,308 keys): zero
`id:` keys for Travis tccsearch, Bell probate and Williamson probate; 773 Travis
liens keyed `addr:lien|travis|<debtor name>|` (a second lien against the same
person was filtered forever); probate keyed only via `parcel:` or a degenerate
`addr:probate|<county>||`. Fixed 2026-10-27 in data_formatter._notice_id_from_url.

Run: PYTHONPATH=src python -m pytest tests/test_dedup_keys.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_formatter import _notice_id_from_url, deduplicate  # noqa: E402
from main import _notice_dedup_key  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

TCC_LEGACY = "https://www.tccsearch.org/RealEstate/DocumentDetail.aspx?InstrumentNumber=202641242"
TCC_NEW = "https://www.tccsearch.org/RealEstate/SearchEntry.aspx#InstrumentNumber=202641242"
BELL_PROBATE = "https://bell.tx.publicsearch.us/?instNum=2026030172"
BELL_LIEN = "https://bell.tx.publicsearch.us/doc/2026030172"
WILCO_LEGACY = "https://judicialrecords.wilco.org/PublicAccess/CaseDetail.aspx?CaseID=2577925"
WILCO_NEW = "https://judicialrecords.wilco.org/PublicAccess/default.aspx#CaseID=2577925&case=24-0468-CP4"
WILCO_SEARCH = "https://judicialrecords.wilco.org/PublicAccess/Search.aspx?ID=200"
TYLER = "https://williamsoncountytx-web.tylerhost.net/williamsonweb/document/DOC842S8132"
ODYSSEY_DETAILS = "https://www.tccsearch.org/Details.aspx?SID=abc&ID=509975"


def test_travis_legacy_and_fragment_urls_same_id():
    assert _notice_id_from_url(TCC_LEGACY) == "www.tccsearch.org/instrument/202641242"
    assert _notice_id_from_url(TCC_NEW) == _notice_id_from_url(TCC_LEGACY)


def test_bell_probate_instnum_matches_lien_doc_form():
    assert _notice_id_from_url(BELL_PROBATE) == "bell.tx.publicsearch.us/doc/2026030172"
    assert _notice_id_from_url(BELL_LIEN) == _notice_id_from_url(BELL_PROBATE)


def test_wilco_caseid_legacy_and_fragment():
    assert _notice_id_from_url(WILCO_LEGACY) == "judicialrecords.wilco.org/case/2577925"
    assert _notice_id_from_url(WILCO_NEW) == _notice_id_from_url(WILCO_LEGACY)


def test_search_aspx_id_200_never_keys():
    """A stored search URL must never become a shared key (would collapse a feed)."""
    assert _notice_id_from_url(WILCO_SEARCH) == ""
    assert _notice_id_from_url("https://x.gov/Search.aspx?ID=200&foo=bar") == ""


def test_odyssey_details_id_still_keys_and_tyler_unchanged():
    assert _notice_id_from_url(ODYSSEY_DETAILS) == "509975"
    assert _notice_id_from_url(TYLER) == "williamsoncountytx-web.tylerhost.net/document/DOC842S8132"


def test_domain_qualified_instruments_do_not_collide_across_counties():
    a = _notice_id_from_url("https://bell.tx.publicsearch.us/doc/2026000001")
    b = _notice_id_from_url("https://williamson.tx.publicsearch.us/doc/2026000001")
    assert a != b


def test_unmatched_shapes_return_empty():
    for u in ("", "photo:IMG_1.jpg", "https://apps.wilco.org/countyclerk/trustee_sales/2026-11/files.aspx",
              "https://tax-office.traviscountytx.gov/properties/0123", "https://mvbalaw.com/tax-sales/"):
        assert _notice_id_from_url(u) == "", u


def test_notice_dedup_key_prefers_id_over_parcel_and_name():
    n = NoticeData(notice_type="lien", county="Travis", owner_name="John Smith",
                   tax_owner_name="SMITH JOHN", parcel_id="P1", source_url=TCC_LEGACY)
    assert _notice_dedup_key(n) == "id:www.tccsearch.org/instrument/202641242"


def test_two_liens_same_debtor_distinct_keys():
    """The production bug: both liens against John Smith used to share
    `addr:lien|travis|john smith|` and the second was filtered as seen."""
    a = NoticeData(notice_type="lien", county="Travis", owner_name="John Smith",
                   source_url="https://www.tccsearch.org/RealEstate/SearchEntry.aspx#InstrumentNumber=1")
    b = NoticeData(notice_type="lien", county="Travis", owner_name="John Smith",
                   source_url="https://www.tccsearch.org/RealEstate/SearchEntry.aspx#InstrumentNumber=2")
    assert _notice_dedup_key(a) != _notice_dedup_key(b)
    assert _notice_dedup_key(a).startswith("id:")


def test_probate_without_address_keys_on_case_not_degenerate():
    n = NoticeData(notice_type="probate", county="Williamson", decedent_name="Joe Doe",
                   source_url=WILCO_LEGACY)
    assert _notice_dedup_key(n) == "id:judicialrecords.wilco.org/case/2577925"


def test_probate_id_wins_over_type_agnostic_parcel():
    """Probate used to seal `parcel:X` — colliding with a tax-delinquent record
    on the same parcel sealed earlier. The case id now comes first."""
    n = NoticeData(notice_type="probate", county="Williamson", decedent_name="Joe Doe",
                   parcel_id="R123456", source_url=WILCO_LEGACY)
    assert _notice_dedup_key(n).startswith("id:")


# ── Working URLs + the human-readable reference in Notes (bug: dead deep links) ──

def test_instrument_url_is_search_page_with_fragment_and_same_id():
    from scrapers.tccsearch_common import instrument_url
    u = instrument_url("202641242")
    assert u.startswith("https://www.tccsearch.org/RealEstate/SearchEntry.aspx#")
    assert "DocumentDetail" not in u                     # the dead deep link is gone
    assert _notice_id_from_url(u) == "www.tccsearch.org/instrument/202641242"


def test_case_url_is_landing_page_with_caseid_fragment():
    from scrapers.probate_odyssey import case_url, WILCO_BASE
    u = case_url(WILCO_BASE, "CaseDetail.aspx?CaseID=2577925", "24-0468-CP4")
    assert u == "https://judicialrecords.wilco.org/PublicAccess/default.aspx#CaseID=2577925&case=24-0468-CP4"
    assert "Search.aspx?ID=" not in u
    assert _notice_id_from_url(u) == "judicialrecords.wilco.org/case/2577925"
    # unknown shapes pass through unchanged
    assert case_url(WILCO_BASE, "Other.aspx?x=1") == f"{WILCO_BASE}/Other.aspx?x=1"


def test_travis_scrapers_emit_working_urls():
    """Every tccsearch scraper routes through instrument_url (no dead links)."""
    import inspect
    import scrapers.foreclosure_travis as f
    import scrapers.probate_travis as p
    import scrapers.lien_travis as l
    import scrapers.lis_pendens_travis as lp
    for mod in (f, p, l, lp):
        src = inspect.getsource(mod)
        assert "DocumentDetail.aspx?InstrumentNumber" not in src.replace(
            "RealEstate/DocumentDetail.aspx?InstrumentNumber=N", ""), mod.__name__
        assert "instrument_url(instrument)" in src, mod.__name__
    import scrapers.probate_bell as pb
    assert "/doc/{row.get('instNum', '')}" in inspect.getsource(pb)


def test_notes_carry_source_reference_labels():
    from datasift_formatter import _source_ref_label, _build_property_section
    from scrapers.tccsearch_common import instrument_url
    lien = NoticeData(notice_type="lien", county="Travis", owner_name="A B", zip="78701",
                      lien_type="Abstract Of Judgment", source_url=instrument_url("202641242"))
    sec = _build_property_section(lien)
    assert "Travis Clerk Instrument # 202641242 (tccsearch.org" in sec
    bell = NoticeData(notice_type="probate", county="Bell", decedent_name="X Y",
                      source_url="https://bell.tx.publicsearch.us/doc/2026030172")
    assert _source_ref_label(bell) == "Bell Clerk Instrument # 2026030172 (bell.tx.publicsearch.us)"
    wil = NoticeData(notice_type="probate", county="Williamson", decedent_name="X Y",
                     case_number="24-0468-CP4", source_url=WILCO_NEW)
    sec = _build_property_section(wil)
    assert "Williamson CCL4 Case No. 24-0468-CP4" in sec
    assert sec.count("24-0468-CP4") == 1 or "Case No. 24-0468-CP4 |" not in sec  # no duplicate line
    # tax sale: no portal label, plain Case No. from case_number
    ts = NoticeData(notice_type="tax_sale", county="Travis", case_number="GN21006856",
                    source_url="https://travis.texas.realforeclose.com/index.cfm?zaction=AUCTION")
    assert "Case No. GN21006856" in _build_property_section(ts)
    assert _source_ref_label(ts) == ""


def test_deduplicate_collapses_same_instrument_across_url_forms():
    old = NoticeData(notice_type="foreclosure", county="Travis", address="1 Main St", source_url=TCC_LEGACY)
    new = NoticeData(notice_type="foreclosure", county="Travis", address="1 Main St", source_url=TCC_NEW)
    other = NoticeData(notice_type="lien", county="Travis", owner_name="A B",
                       source_url="https://www.tccsearch.org/RealEstate/SearchEntry.aspx#InstrumentNumber=9")
    out = deduplicate([old, new, other])
    assert len(out) == 2

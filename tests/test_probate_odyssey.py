"""Williamson probate (Odyssey PublicAccess) — filter, counters, case page parse.

Fixtures under tests/fixtures/ are the live `document.body.innerText` of real
Register-of-Actions pages captured 2026-10-27 (public court records). The
party rows below are the structured output of the scraper's JS extraction for
the same pages (th.ssTableHeader role + name, then the <td> cells).

Run: PYTHONPATH=src python -m pytest tests/test_probate_odyssey.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import NoticeData  # noqa: E402
import scrapers.probate_odyssey as po  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ── grid-row filter ──────────────────────────────────────────────────

def test_is_probate_case_terminal_cp4():
    assert po._is_probate_case("26-1168-CP4", "Estate Of X, Deceased", "Letters Testamentary")
    assert po._is_probate_case("24-0468-CP4", "", "")          # number alone suffices
    assert po._is_probate_case("24-0468-cp4 ", "", "")         # case/space tolerant


def test_subcase_cp4a_rejected_without_estate_title():
    # Adversary sub-case inside a probate: "Frank P. Verock v. Tawanna Kay Verock"
    assert not po._is_probate_case("24-0468-CP4A", "Frank P. Verock v. Tawanna Kay Verock", "Probate")
    # ...but a sub-case whose title/type says estate still qualifies
    assert po._is_probate_case("24-0468-CP4A", "Estate Of Joe Verock, Deceased", "Other")
    assert po._is_probate_case("24-0468-CP4A", "", "Muniment Of Title")


def test_non_probate_civil_rows_rejected():
    assert not po._is_probate_case("26-0001-C425", "Smith v. Jones", "Debt Claim")
    assert not po._is_probate_case("26-0002-F395", "In the Matter of the Marriage of", "Divorce")


def test_build_notice_sets_case_number_and_decedent():
    rec = {
        "case_number": "26-1168-CP4",
        "case_title": "Estate Of \nJason Michael Stiles,\nDeceased",
        "filed_date": "10/07/2026", "court": "County Court at Law #4",
        "case_type": "Independent Administration w/Heirship",
        "detail_url": "CaseDetail.aspx?CaseID=2579752", "is_probate": True,
    }
    n = po._build_notice(rec, po.WILCO_BASE, "Williamson")
    assert n is not None
    assert n.case_number == "26-1168-CP4"
    assert n.decedent_name == "Jason Michael Stiles"
    assert n.date_added == "2026-10-07"
    assert n.owner_name == ""            # PR comes from the case page
    assert "Case: 26-1168-CP4" in n.raw_text


def test_build_notice_drops_guardianship_and_adversary():
    guard = {"case_number": "26-1170-CP4", "case_title": "Guardianship Of \nB. Lowe,\nAn Incapacitated Person",
             "filed_date": "10/07/2026", "court": "", "case_type": "Guardianship Person - Adult",
             "detail_url": "x", "is_probate": True}
    assert po._build_notice(guard, po.WILCO_BASE, "Williamson") is None
    adv = {"case_number": "24-0468-CP4A", "case_title": "Frank P. Verock v. Tawanna Kay Verock",
           "filed_date": "10/12/2026", "court": "", "case_type": "Probate",
           "detail_url": "x", "is_probate": True}
    assert po._build_notice(adv, po.WILCO_BASE, "Williamson") is None


# ── case page parse ──────────────────────────────────────────────────

PARTIES_26_1168 = [
    {"role": "Applicant", "name": "Stiles, James F.",
     "details": ["Ted W Hejl\n\xa0\xa0Retained\n512-365-6348(W)"]},
    {"role": "Decedent", "name": "STILES, JASON MICHAEL",
     "details": ["White\nDOD: 07/24/2026\n6'\xa01\", 190 lbs"]},
    {"role": "Heir", "name": "**Confidential**", "details": ["0'\xa00\", 0 lbs"]},
    {"role": "Heir", "name": "Unknown Heirs",
     "details": ["Joseph S. Babb\n\xa0\xa0Court Appointed\n512-580-5010(W)"]},
]

PARTIES_24_0468 = [
    {"role": "Administrator", "name": "Verock, Tawanna Kay",
     "details": ["Don E Walden\n\xa0\xa0Retained\n512-349-9595(W)"]},
    {"role": "Decedent", "name": "Verock, Joseph Roland", "details": ["DOD: 01/06/2024"]},
]


def test_party_name_to_first_last():
    f = po.party_name_to_first_last
    assert f("Verock, Tawanna Kay") == "Tawanna Kay Verock"
    assert f("STILES, JASON MICHAEL") == "Jason Michael Stiles"
    assert f("Li, Felisa Tan Also Known As Li, Felisa T.") == "Felisa Tan Li"
    assert f("**Confidential**") == ""
    assert f("Unknown Heirs") == ""
    assert f("") == ""


def test_party_name_suffix_and_entity_not_reversed():
    """Review finding: a trailing ', Jr.' is a suffix (not a second given name)
    and an entity PR is never flipped — both would otherwise be re-flipped by the
    formatter into a reversed First/Last (the NAMELF rule)."""
    f = po.party_name_to_first_last
    assert f("Smith, John, Jr.") == "John Smith Jr"
    assert f("SMITH, JOHN, III") == "John Smith Iii" or f("SMITH, JOHN, III").startswith("John Smith")
    assert f("Wells Fargo Bank, N.A.") == "Wells Fargo Bank, N.A."
    assert f("Frost Bank, Trustee") == "Frost Bank, Trustee"


def test_parse_events_stops_at_financial_information():
    body = ("Register of Actions\nCase No. 26-0001-CP4\n"
            "Events & Orders of the Court\n\xa0\t\xa0\t\xa0\tOTHER EVENTS AND HEARINGS\n"
            "10/07/2026\t\xa0\t\xa0\tApplication for Letters Testamentary\n"
            "10/20/2026\t\xa0\t\xa0\tOrder Admitting Will and Granting Letters Testamentary\n"
            "Financial Information\n"
            "11/01/2026\tTransaction Assessment\t$400.00\n"
            "11/02/2026\tLetters fee payment\t$2.00\n")
    d = po.parse_case_detail([], body)
    assert [t for _, t in d["events"]] == ["Application for Letters Testamentary",
                                           "Order Admitting Will and Granting Letters Testamentary"]
    assert d["last_event_date"] == "2026-10-20"
    assert d["letters_date"] == "2026-10-20"


def test_last_meta_returned_means_probate_candidates():
    """run_health's parse-zero alarm is 'returned>0, kept==0'. `returned` must be
    the probate candidates, not every civil row, or an estate-free window would
    read as a grid regression. (grid_rows keeps the all-rows total.)"""
    import inspect
    src = inspect.getsource(po.WilliamsonProbateScraper.scrape)
    assert 'self.last_meta["grid_rows"] = total' in src
    assert 'self.last_meta["returned"] = len(probate_records)' in src
    assert 'self.last_meta["returned"] = total' not in src


def test_parse_case_detail_applicant_attorney_dod():
    d = po.parse_case_detail(PARTIES_26_1168, _fixture("wilco_casedetail_26-1168-CP4.txt"))
    assert d["case_number"] == "26-1168-CP4"
    assert d["pr_name"] == "James F. Stiles"
    assert d["pr_role"] == "Applicant"
    assert d["attorney_name"] == "Ted W Hejl"
    assert d["attorney_phone"] == "512-365-6348"
    assert d["date_of_death"] == "2026-07-24"
    assert d["decedent_name"] == "Jason Michael Stiles"
    # the Court-Appointed ad litem on the "Unknown Heirs" row is NOT the PR's attorney
    assert "Babb" not in d["attorney_name"]
    # demographics on the decedent row are never captured anywhere
    assert "White" not in str(d) and "190 lbs" not in str(d)
    # events: fresh filing, no letters yet
    assert d["events"] and d["last_event_date"] >= "2026-10-22"
    assert d["letters_date"] == ""
    assert all(e[0].startswith("2026-10") for e in d["events"])


def test_parse_case_detail_administrator_with_letters():
    d = po.parse_case_detail(PARTIES_24_0468, _fixture("wilco_casedetail_24-0468-CP4.txt"))
    assert d["case_number"] == "24-0468-CP4"
    assert d["pr_name"] == "Tawanna Kay Verock"
    assert d["pr_role"] == "Administrator"
    assert d["attorney_name"] == "Don E Walden"
    assert d["attorney_phone"] == "512-349-9595"
    assert d["date_of_death"] == "2024-01-06"
    assert d["letters_date"] == "2025-06-03"          # Order Granting Letters of Admin ...
    # "Inventory / Affidavit Due" (06/12) is skipped; the real "Inventory" filing is 09/04
    assert d["inventory_date"] == "2025-09-04"
    assert any(t.startswith("Order Granting Letters") for t in d["dispositions"])
    assert d["last_event_date"] >= "2025-07-08"


def test_pr_role_priority_prefers_executor_over_applicant():
    parties = [
        {"role": "Applicant", "name": "Doe, Jane", "details": []},
        {"role": "Independent Executor", "name": "Doe, John", "details": ["A Lawyer Retained 512-000-0000(W)"]},
        {"role": "Attorney Ad Litem", "name": "Smith, Sam", "details": []},
        {"role": "Decedent", "name": "Doe, Pat", "details": ["DOD: 1/2/2026"]},
    ]
    d = po.parse_case_detail(parties, "Register of Actions\nCase No. 26-0001-CP4\n")
    assert d["pr_name"] == "John Doe" and "Executor" in d["pr_role"]
    assert d["date_of_death"] == "2026-01-02"


def test_apply_case_detail_sets_owner_dod_and_notes():
    n = NoticeData(notice_type="probate", county="Williamson", decedent_name="Jason Michael Stiles",
                   case_number="26-1168-CP4", raw_text="Case: 26-1168-CP4")
    d = po.parse_case_detail(PARTIES_26_1168, _fixture("wilco_casedetail_26-1168-CP4.txt"))
    po.apply_case_detail(n, d)
    assert n.owner_name == "James F. Stiles"            # PR, FIRST LAST — never the decedent
    assert n.decedent_name == "Jason Michael Stiles"
    assert n.date_of_death == "2026-07-24" and n.owner_deceased == "yes"
    assert n.decision_maker_name == "James F. Stiles"
    assert n.decision_maker_relationship == "applicant"
    assert n.decision_maker_source == "court_record"
    assert "Attorney: Ted W Hejl 512-365-6348" in n.raw_text
    assert "PR: James F. Stiles (Applicant)" in n.raw_text
    assert "Docket:" in n.raw_text
    # The fields that actually reach DataSift Notes (raw_text does not):
    assert n.case_attorney == "Ted W Hejl 512-365-6348"
    assert n.case_docket.startswith(f"{len(d['events'])} events, last ")
    from datasift_formatter import _build_row
    n.address, n.city, n.zip = "200 W Nash St", "Round Rock", "78664"
    row = _build_row(n)
    assert "Attorney: Ted W Hejl 512-365-6348" in row["Notes"]
    assert "Docket:" in row["Notes"]
    assert "Case No. 26-1168-CP4" in row["Notes"]       # no source_url on this notice → plain label
    assert row["Personal Representative"] == "James F. Stiles"
    assert row["Date of Death"] == "2026-07-24"


def test_apply_case_detail_keeps_existing_decision_maker():
    n = NoticeData(notice_type="probate", county="Williamson", decedent_name="X Y",
                   decision_maker_name="Someone Else", decision_maker_relationship="spouse")
    po.apply_case_detail(n, {"pr_name": "Tawanna Kay Verock", "pr_role": "Administrator"})
    assert n.owner_name == "Tawanna Kay Verock"
    assert n.decision_maker_name == "Someone Else"      # not overwritten


def test_probate_preset_fires_with_court_pr():
    """owner_name (PR) + decedent_name → the obituary probate preset condition
    (obituary_enricher.py) is now satisfied for Williamson records."""
    from obituary_enricher import _is_same_person
    n = NoticeData(notice_type="probate", owner_name="James F. Stiles", decedent_name="Jason Michael Stiles")
    assert n.owner_name and n.decedent_name and not _is_same_person(n.owner_name, n.decedent_name)


def test_scraper_has_last_meta_before_run():
    s = po.WilliamsonProbateScraper()
    assert isinstance(s.last_meta, dict)
    assert s.fetch_details is True

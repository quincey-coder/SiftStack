"""Offline tests for the Travis/Bell Odyssey Portal docket reader (src/odyssey_portal.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import odyssey_portal as op  # noqa: E402


def _party(pid, name, dob, cases):
    rows = "".join(
        f'<tr><td class="party-case-caseid"><a class="caseLink" data-url="/app/ROA/?id={n}">{n}</a></td>'
        f'<td class="party-case-style"><div title="{style}">{style}</div></td>'
        f'<td class="party-case-type">{typ}</td><td class="party-case-status">{st}</td>'
        f'<td class="party-case-location">Probate Court #2</td></tr>' for n, style, typ, st in cases)
    return (f'<tr><td></td><td><a class="nolink" data-party-id="{pid}">{name}</a></td><td>{dob}</td></tr>'
            f'<tr><td><div class="party-card" data-party-id="{pid}"><table>{rows}</table></div></td></tr>')


HTML = "<table>" + _party("A", "Smith, Eleanor A", "08/05/1914", [
    ("C-1-PB-25-000672", "IN THE ESTATE OF Eleanor A. Smith, DECEDENT", "Other Estate Proceedings", "Active"),
    ("D-1-FM-14-003007", "IN THE MATTER OF THE MARRIAGE OF FRANK SMITH VS. ELEANOR SMITH", "Divorce", "Closed"),
    ("T-1", "STATE VS SMITH", "Traffic", "Closed")]) + _party("B", "SMITH, ELEANOR JO", "", [
    ("C-1-PB-14-001897", "ELEANOR JO SMITH", "Probate Will and Issuance of Letters Testamentary", "Closed")]) + \
    _party("C", "SMITH, ELLEN", "", [("C-1-PB-1", "ELLEN SMITH", "Independent Administration", "Closed")]) + \
    _party("D", "Garcia, Pedro", "XX/XX/1949", [
        ("25DCV357354", "Tax Appraisal District of Bell County VS. Matilde Garcia,Pedro Garcia", "Tax Delinquency",
         "Active")]) + "</table>"


def test_parse_and_classify_keeps_estates_and_civil_cases_of_the_name():
    parties = op.parse_results(HTML)
    assert [p["name"] for p in parties] == ["Smith, Eleanor A", "SMITH, ELEANOR JO", "SMITH, ELLEN", "Garcia, Pedro"]
    res = op.classify(parties, "ELEANOR", "SMITH")
    assert [c["case_number"] for c in res["cases"]] == ["C-1-PB-25-000672", "C-1-PB-14-001897"]
    assert res["cases"][0]["style"] == "Estate Of Eleanor A. Smith, Deceased"
    assert res["cases"][0]["party_birth_year"] == 1914
    assert res["cases"][1]["style"] == "Estate Of Eleanor Jo Smith"      # caption was just the name
    assert [c["case_number"] for c in res["other"]] == ["D-1-FM-14-003007"]   # traffic dropped
    tax = op.classify(parties, "PEDRO", "GARCIA")["other"][0]
    assert tax["party_birth_year"] == 1949 and "[party: Pedro Garcia]" in tax["style"]


BELL_DETAIL = """Print
Case Information
Party
Events and Hearings
Case Information
25DCV357354 | Tax Appraisal District of Bell County VS. Matilde Garcia,Pedro Garcia
File Date
10/30/2025
Case Type
Tax Delinquency
Case Status
Active
Party
Plaintiff
Tax Appraisal District of Bell County
Active Attorneys
Lead Attorney
ALLEN, HARVEY M.
Defendant
Garcia, Pedro
Defendant
Beabout, Dude, SR
Events and Hearings
10/30/2025 New Case Filed (OCA)
11/21/2025 Unserved Service
"""

TRAVIS_DETAIL = """Case No. C-1-PB-25-000672
IN THE ESTATE OF Eleanor A. Smith, DECEDENT
Filed on:
03/26/2025
Case Type:\tOther Estate Proceedings
Subtype:\tDetermination of Heirship (No Admin)
Case Events
03/26/2025
OPN:PB ALL OTHER ESTATE PROCEEDINGS
10/16/2026
ISS:PB LETTER OF TEMPORARY ADMINISTRATION
"""


def test_parse_detail_both_layouts():
    b = op.parse_detail(BELL_DETAIL)
    assert b["filed"] == "2025-10-30" and b["case_type"] == "Tax Delinquency"
    assert [p["name"] for p in b["parties"]] == ["Tax Appraisal District Of Bell County", "Pedro Garcia",
                                                 "Dude Beabout Sr"]
    assert b["attorney_name"] == "Harvey M. Allen"
    assert b["events"][0] == "2025-10-30 New Case Filed (OCA)"
    t = op.parse_detail(TRAVIS_DETAIL)
    assert t["filed"] == "2025-03-26" and t["subtype"].startswith("Determination of Heirship")
    assert t["letters_date"] == "2026-10-16" and t["last_event_date"] == "2026-10-16"


def test_classify_matches_two_part_surname_party():
    html = "<table>" + _party("E", "GARCIA LOPEZ, MARIA DEL CARMEN", "", [
        ("C-1-PB-2", "MARIA DEL CARMEN GARCIA LOPEZ", "Independent Administration", "Active")]) + "</table>"
    res = op.classify(op.parse_results(html), "MARIA", "GARCIA")
    assert [c["case_number"] for c in res["cases"]] == ["C-1-PB-2"]


def test_bare_caption_names_the_decedent_not_the_searched_heir():
    html = "<table>" + _party("A", "SMITH, ELEANOR C", "", [
        ("C-1-PB-10-1", "NICHOLS ELIZABETH", "Probate Will and Issuance of Letters Testamentary", "Closed"),
        ("C-1-PB-11-2", "WRIGHT DUNCAN", "Civil Action (Probate Civil Action/ Ancilliary/736)", "Closed")]) + "</table>"
    res = op.classify(op.parse_results(html), "ELEANOR", "SMITH")
    assert [c["style"] for c in res["cases"]] == ["Estate Of Elizabeth Nichols"]   # Eleanor is the applicant
    assert [c["case_number"] for c in res["other"]] == ["C-1-PB-11-2"]              # Rule 736 = foreclosure


def test_bare_caption_orientation():
    assert op._style_decedent("GREEN RUBY", "GREEN") == "Ruby Green"
    assert op._style_decedent("ALBERT L AMEZQUITA", "AMEZQUITA") == "Albert L Amezquita"
    assert op._style_decedent("VIVIAN D GOMEZ AKA", "GOMEZ") == "Vivian D Gomez"
    assert op._style_decedent("SMITH JOHN A", "JONES") == "John A Smith"
    assert op._style_decedent("NICHOLS ELIZABETH", "SMITH") == "Elizabeth Nichols"

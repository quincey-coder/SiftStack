"""Curative custom-field values: labels stay exact and links land in both the fields and Notes."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import curative_fields as cf


def _assessment(**kw):
    return {"tier": "B", "score": 58, "spread": 124632, "death_proof": "court/recorded", **kw}


def test_headers_match_field_definitions_letter_for_letter():
    defs = json.loads(cf.DEFINITIONS_PATH.read_text(encoding="utf-8"))
    assert list(cf.values({}, _assessment()))[:len(defs["fields"])] == [f["label"] for f in defs["fields"]]


def test_select_values_are_listed_options():
    defs = {f["label"]: f.get("options") for f in json.loads(cf.DEFINITIONS_PATH.read_text(encoding="utf-8"))["fields"]}
    for proof in ("court/recorded", "county roll estate label", "obituary + memorial", "obituary only",
                  "memorial only", "none"):
        assert cf.values({}, _assessment(death_proof=proof))["Death Proof"] in defs["Death Proof"]
    assert cf.values({}, _assessment())["Ghost Payer Strength"] == "None"
    strong = _assessment(ghosts=[{"strength": "strong (separate payor record in that name)"}])
    assert cf.values({}, strong)["Ghost Payer Strength"] == "Strong"


def test_links_only_where_they_open_directly():
    bell = cf.values({"county": "Bell", "apn": "349852"}, _assessment(probate=[{"url": "https://x.gov/Case"}]))
    assert bell["CAD Link"] == "https://belltx-search.gsacorp.io/parcel/0000349852"
    assert bell["Tax Link"] == "https://belltx-search.gsacorp.io/tax/0000349852"
    assert bell["Probate Link"] == "https://x.gov/Case"
    wilco = cf.values({"county": "Williamson", "apn": "r020211"}, _assessment(probate=[{"href": "CaseDetail.aspx?CaseID=1"}]))
    assert wilco["CAD Link"] == "" and wilco["Probate Link"] == ""
    assert wilco["Tax Link"].endswith("PropertyQuickRefID=R020211")
    travis = cf.values({"county": "Travis", "apn": "02242410140000", "value": {"account": "02242410140000"}}, _assessment())
    assert travis["CAD Link"] == ""   # 14 digits is the Tax Office account, not the TCAD PID
    assert travis["Tax Link"].endswith("account=02242410140000")


def test_links_match_the_field_values():
    a = _assessment(memorials=[{"id": 7}])
    rec = {"county": "Bell", "apn": "349852"}
    urls = cf.links(rec, a)
    assert urls["Grave Link"] == "https://www.findagrave.com/memorial/7"
    assert all(cf.values(rec, a)[k] == v for k, v in urls.items())


def test_ruled_out_obituary_never_reaches_the_field():
    a = _assessment(ruled_out=["obituary ruled out: a namesake"])
    assert cf.values({}, a, {"obituary_url": "https://funeral.example/x"})["Obituary URL"] == ""
    assert cf.values({}, _assessment(), {"obituary_url": "https://funeral.example/x"})["Obituary URL"] == "https://funeral.example/x"


def test_owner_deceased_means_the_owner_of_record():
    prior = _assessment(dead=[{"name": "Ira Crofford Jr", "dead_by": "2008-04-12"}], owner_dead=False)
    v = cf.values({}, prior)
    assert v["Owner Deceased"] == "" and v["Decedent Name"] == "Ira Crofford Jr"
    own = _assessment(dead=[{"name": "James D Lee", "dead_by": "2022-10-22"}], owner_dead=True)
    assert cf.values({}, own)["Owner Deceased"] == "Yes"
    memorial = {**own, "death_proof": "memorial only"}
    assert cf.values({}, memorial)["Owner Deceased"] == "Suspected"


def test_name_only_liens_are_labelled_and_fragment_heirs_are_not_counted():
    lien = {"class": "judgment", "creditor": "CAPITAL ONE", "instrument": "1", "on_property": "unknown"}
    same = {"class": "mortgage", "creditor": "SWIFT", "instrument": "2", "on_property": "same"}
    v = cf.values({}, _assessment(clerk_open=[lien, same], legal="L1 B1 SADDLERIDGE", heirs=[{"name": "Dorothy"}, {"name": "Jeff Medlock"}]))
    assert v["Open Lien Creditors"] == "judgment: CAPITAL ONE #1 (name match only); mortgage: SWIFT #2"
    assert v["Legal Description"] == "L1 B1 SADDLERIDGE"
    assert v["Other Owners / Heirs Names"] == "Jeff Medlock" and v["Heir Count"] == 1


def test_letters_date_falls_back_to_the_docket_event():
    a = _assessment(probate=[{"letters_date": "", "events": ["2023-10-30 Proof of Death", "2023-11-06 Issue Letters of Testamentary"]}])
    assert cf.values({}, a)["Letters Granted Date"] == "2023-11-06"


def test_decedent_takes_the_fullest_spelling_with_the_suffix_last():
    two = _assessment(dead=[{"name": "James Lee", "dead_by": ""}, {"name": "James D Lee", "dead_by": ""}])
    assert cf.values({}, two)["Decedent Name"] == "James D Lee"
    assert cf.values({}, _assessment(dead=[{"name": "Joe S Jr Thompson", "dead_by": ""}]))["Decedent Name"] == "Joe S Thompson Jr"

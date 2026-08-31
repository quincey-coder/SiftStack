"""Austin BSC agenda parser tests (pure functions, no network).

The text constants below are REAL pdfminer output from BSC agendas on EDIMS
(ids 479428 = 2026-08-26, 390266 = 2022-08-24, 366065 = 2021-08-25,
325907 = 2019-08-29, 284338 = 2017-09-27), trimmed to the hearing items.

Run standalone or via pytest:
    PYTHONPATH=src python tests/test_bsc_austin_parsing.py
    PYTHONPATH=src pytest tests/test_bsc_austin_parsing.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import bsc_austin as b  # noqa: E402


AGENDA_2026 = """\
PUBLIC HEARINGS

New Case(s) The Commission will hear the following cases concerning alleged violations of the City’s
Property Maintenance Code and may issue an order to vacate, relocate occupants, repair, demolish,
or secure the premises; and may assess civil penalties:

2.

Case Number: CL 2026-083909
Property address: 9204 Curlew Drive / Owners: Thomas A. Rodriguez & Casimiro Rodriguez
Staff presenter: Brandon Smith

  Staff recommendation: Repair residential structure.

3.

4.

5.

Case Number: CL 2026-083918
Property address: 2932 Jubilee Trail / Owners: Filemon Nava Bravo & Carrie Lyn Bravo
Staff presenter: Brandon Smith

  Staff recommendation: Repair residential structure.

Case Number: CL 2026-076108
Property address: 2507 Burleson Road, aka Burl North/ Owner: Brugge 2507 LLC
Staff presenter: Juan Villareal
Staff recommendation: Repair fire-damaged commercial multi-family structures.

Case Number: CL 2026-090706
Property address: 4508 Raleigh Circle / Owner: Carlos Oviedo c/o Alex Vasquez
Staff presenter: Trey Kleinert
Staff recommendation: Repair residential structure.

Returning Case(s) The Commission will hear the following cases concerning alleged violations of the
City’s  Property  Maintenance  Code  and  may  issue  an  order  to  vacate,  relocate  occupants,  repair,
demolish, or secure the premises; and may assess or modify a civil penalty:

6.

7.

Case Number: CL 2025-141760
Property address:1601 Royal Crest Drive, aka Mia Riverside Apartments / Owner: Mia Riverside
Holdco LLC
Staff Presenter: Phillip Merino
Previous commission action: Order TRV 2025142639 issued December 9, 2025, for repair of
the commercial multi-family structure and obtain and complete a comprehensive evaluation
of the private wastewater system.
Staff  recommendation:  Replace  Order  TRV  2025142639  issued  on  December  9,  2025,  by
issuing  a  new  order  to  vacate  the  property  and  demolish  the  commercial  multi-family
structures. Structures are to remain vacant and secured until demolition is complete. Cap all
accrued penalties resulting from prior order and carry over to new order.

Case Number: CL 2023-103040
Property address: 4606 Connelly Street, aka Zoe’s Safe Place / Owner: Hyde Park Collective
LLC
Staff presenter: John Hale
Previous commission action: Order TRV 2023103040 issued September 27, 2023, for repair of
the commercial structure within 30 days.
Staff recommendation: Uphold accrued penalties from Order issued September 27, 2023.

Appeal(s) The Commission will hear the following appeals and may take action:

8.

Case Number: CV 2026-087034
Property address: 415 E St. Elmo Road / Appellant: Leifelmo415 LLC
Staff presenter: Iesha Young
Staff recommendation: Deny appeal of the notice of violation.

9.

Case Number: CV 2026-086570
Property address: 919 E Koenig Lane, aka Best Western Plus / Owner: 919 East Koenig Lane
Owner LLC / Appellant: Jonathan Reiss, WNW Management LLC
Staff presenter: Sandy Van Cleave
Staff recommendation: Deny appeal of the notice of violation.

DISCUSSION ITEMS

10.

Consider and make a recommendation regarding an ordinance amending City Code Chapter
25-12, Article 9 (Property Maintenance Code) to establish an emergency demolition process.

FUTURE AGENDA ITEMS

ADJOURNMENT
"""

AGENDA_2022 = """\
  PUBLIC HEARINGS

Appeal(s): The Commission will hear the following appeal cases and may take action:

2.  Case Number: CL 2022-117834

Property address: 1901 Rio Grande Street / Owner/Appellants: 19 Rio Partners LP /Ben Turner
Staff presenter:  Edmund Su
Staff recommendation: Deny appeal of the Notice of Violation

Page 1

3.  Case Number: OL 2022-000952

Property address: 6700 Ripple Run/  Owners/Appellants: Ernalee and Brian Duffy
Staff presenter:  Lisa DeRoche
Staff recommendation: Uphold Austin Code’s decision to deny  application for Type-1 Short
Term Rental License

New Case(s): The Commission will hear the following cases concerning alleged violations of the
City’s Property Maintenance Code and may issue an order to vacate, relocate occupants, repair,
demolish, or secure the premises; and may assess civil penalties:

4.  Case Number: CL 2022-117169

Property address: 8408 Briarwood Lane/ Owner: Reagan Rhett Simon
Staff presenter:  Belinda Thompson
Staff recommendation: Demolish residential and accessory structures

5.  Case Number: CL 2022-117754

Property address: 7611 Old Bee Caves Road/ Owner: Thomas Beckett, Jr.
Staff presenter:  Scott Moody
Staff recommendation: Demolish fire-damaged secondary residential structure

6.  Case Number: CL 2022-117738

Property address: / 1908 San Gabriel Street / Owner: Aaron S. Reisfield
Staff presenter:  Khary Dumas
Staff recommendation:  Repair commercial structure

7.  Case Number CL 2022-118167

Property address: / 502 W. Longspur Blvd., Bldg. 4, aka 502 North Apartments /
Owners: Longspur Blvd Apts LLC and 4719 Harmon Ave Holding 3 LLC
Staff presenter:  Eric Finn
Staff recommendation:  Repair Repeat Offender Program commercial multi-family structure

DISCUSSION AND POSSIBLE ACTION ITEMS

8.

 Introduction of Kelly Stillwell, Building Official, Developmental Services Department
"""

AGENDA_2021 = """\
PUBLIC HEARINGS

Returning for Commission Action: The Commission will hear the following returning cases that
concern alleged violations of the City’s Property Maintenance Code and may issue orders to vacate,
relocate occupants, repair, demolish, or secure the premises, and may assess or modify a civil penalty:

1.  Case Number: CL 2017-111475

Property address: 2215 E. Anderson Lane Service Road EB / Owner: Ghassan A. Karim, LLC
Staff presenter: Marlayna Wright, Austin Code Department
Staff recommendation: Issue new order for demolition
Previous Commission action:  Rescinded demolition order issued May 26, 2021; and continued
from June 23, 2021 meeting

Page 3

New Case(s): The Commission will hear the following cases concerning alleged violations of the

City’s Property Maintenance Code and may issue an order to vacate, relocate occupants, repair,
demolish, or secure the premises; and may assess civil penalties:

2.  Case Number: CL 2021-085126

Property address: 3004 E. 12th Street / Owner: Magnolia Christian Church
Staff presenter: Willis Adams
Staff recommendation: Demolish commercial structure
Previous Commission Action: Continued from June 23, 2021 meeting

3.  Case Numbers: CL 2021-104102; CL 2021-104106; CL 2021-104358; CL and CL 2021-104363
Property address 6901 Wentworth Drive, Exterior, and Units A, B and D / Owner: William and
Guisela Morrow
Staff presenter: Johnny Serna
Staff recommendation: Repair Repeat Offender Program multi-family commercial structure

6.  Case Numbers:  CL 2021-119634; CL 2021-119642; CL 2021-119650; CL 2021-119667; CL

2021-119728; CL 2021-119730; CL 2021-119734; CL 2021-119736; CL 2021-119758; CL
2021-119760 and CL 2021-119764
8800 N.  IH 35, Buildings 1-10 (Exteriors); and Building 10 (Fire Damage), aka Starburst
Apartments/ Owner: 8800 Austin, LLC
Staff presenter: Johnny Serna, Austin Code Department
Staff recommendation: Repair Repeat Offender Program multi-family commercial structures

DISCUSSION AND POSSIBLE ACTION ITEMS
"""

AGENDA_2019 = """\
  PUBLIC HEARINGS

Case(s): The Commission will hear the following cases concerning alleged violations of the City’s
Property Maintenance Code and may issue an order to vacate, relocate occupants, repair, demolish, or
secure the premises; and may assess civil penalties:

3.   Case Number: CL 2019-163305

Property address: 1619 S. 1st Street aka Jovita’s / Owner: Chingon International LLC
Staff presenter: Sandy Van Cleve, Austin Code Department
Staff recommendation: Demolish commercial structure

Page 1

6.   Case Number: CL 2019-163321

Property address: 2611 Francisco Street /  Owners: Julian and Carmen Garcia
Staff presenter: Alicia Tovar, Austin Code Department
Staff recommendation: Demolish front primary residential structure and front accessory structure

7.  Case Number CL 2019-166776

Property address: 1204 E. St. Johns Avenue / Owner: Nathaniel Aniekwu
Staff presenter: Khary Dumas, Austin Code Department
Staff recommendation:  Repair residential structure

Returning Case(s): The Commission will hear the following returning cases and may issue orders to vacate,

relocate occupants, repair, demolish, or secure the premises, or modify a civil penalty:

8.  Case Number: CL 2019-088180

Property Address: 8573 Red Willow Drive / Owners: Robert V. Armstrong and Joy Panian
Armstrong
Staff presenter: Joseph Lucas, Austin Code Department
Staff recommendation: Repair residential structure

Update Case(s):  The Commission will hear an update on the following cases:

9.   Case Numbers: CL 2019-144381; CL 2019-144429; CL 2019-144432; CL 2019-144397;

CL 2019-144439; CL 2019-144446; CL 2019-144850; CL and 2019-144426
Property address: 1124 Rutland Drive aka Creeks Edge Apartments, Buildings 10, 13, 15, 16, 17
and 18 – Exterior; and Building 12 – Units 101 (Interior) and 102 (Exterior)
Owner: CSC North Austin Realty LLC
Staff presenter: Justin Brummer, Austin Code Department

  DISCUSSION AND POSSIBLE ACTION ITEMS
"""

AGENDA_2017 = """\
  PUBLIC HEARINGS

Case(s): The Commission will hear the following cases concerning alleged violations of the City’s
Property Maintenance Code and may issue an order to vacate, relocate occupants, repair, demolish, or
secure the premises; and may assess civil penalties:

3.  Case Number(s): CL 2017-111459

Property Address/ Owner: 5803 Sweeney Circle, Unit D / Community Partnership for the
Homeless, Inc.

Page 1

Presenter: Khary Dumas, Austin Code Department
Staff recommendation: Repairs

5.  Case Number(s): CL 2017-111451

Property Address/ Owner: 3138 Manor Road / Kennan Properties, LLC
Presenter: Marlayna Wright, Austin Code Department
Staff recommendation: Repairs

  Appeal(s): The Commission will hear the following appeals cases and may take action on the appeal:

7.  Case Number(s): OL 2013-002400

Property Address/ Owner: 605 W. Elizabeth Street / Elisa Jazan and Derek O’Hagan
Presenter: Marcus Elliott, Austin Code Department

  DISCUSSION AND POSSIBLE ACTION
"""


def _by_case(items):
    return {it.case_ids[0]: it for it in items}


# -- normalization --------------------------------------------------------
def test_normalize_joins_wrapped_and_spaced_case_numbers():
    t = b._normalize_text("Case Number: CL 2024- 076044\nfoo CL 2020-\n136461 bar\nPage 2\n\n3. \n\nx￾y")
    assert "CL 2024-076044" in t
    assert "CL 2020-136461" in t
    assert "Page 2" not in t
    assert "\n3." not in t and "3.\n" not in t
    assert "xy" in t


def test_normalize_ascii_punctuation():
    t = b._normalize_text("Zoe’s – Exterior “q”")
    assert t == "Zoe's - Exterior \"q\""


def test_canonicalize_label_variants():
    cases = {
        "Case Number: CL 2026-1": "Case Number: CL 2026-1",
        "Case Numbers: CL 2026-1": "Case Number: CL 2026-1",
        "Case Number(s): CL 2026-1": "Case Number: CL 2026-1",
        "Case Number CL 2026-1": "Case Number: CL 2026-1",
        "7.  Case Number CL 2026-1": "Case Number: CL 2026-1",
        "Property address: 1 A St": "Property Address: 1 A St",
        "Property Address: 1 A St": "Property Address: 1 A St",
        "Property address 1 A St": "Property Address: 1 A St",
        "Property address:1221 Algarita": "Property Address: 1221 Algarita",
        "Property address: / 1908 San Gabriel": "Property Address: 1908 San Gabriel",
        "Property Address/ Owner: 1 A St / Bob": "Property Address: 1 A St / Bob",
        "Staff recommendation: X": "Staff Recommendation: X",
        "Staff Recommendation: X": "Staff Recommendation: X",
        "Staff purpose: X": "Staff Recommendation: X",
        "Staff presenter: X": "Staff Presenter: X",
        "Staff Presenter: X": "Staff Presenter: X",
        "Presenter: X": "Staff Presenter: X",
        "Previous commission action: X": "Previous Commission Action: X",
        "Previous Commission Action: X": "Previous Commission Action: X",
    }
    for raw, want in cases.items():
        got = b._canonicalize_labels(raw)
        assert got == want, f"{raw!r} -> {got!r}, want {want!r}"
    inline = b._canonicalize_labels("x / Owners: A / Owner/Appellants: B / Appellant: C")
    assert inline == "x / Owner: A / Owner: B / Appellant: C"
    # "Owner LLC" (no colon) must NOT become a label
    assert b._canonicalize_labels("919 East Koenig Lane Owner LLC") == "919 East Koenig Lane Owner LLC"


def test_section_sentinels():
    t = b._canonicalize_labels(
        "New Case(s) The Commission will hear\nReturning Case(s): x\nUpdate Case(s):\nAppeal(s) y\n"
        "Case(s): z\nReturning for Commission Action: q\nDISCUSSION ITEMS\nADJOURNMENT"
    )
    names = [ln.split()[-1] for ln in t.split("\n") if ln.startswith("## SECTION")]
    assert names == ["new", "returning", "update", "appeal", "new", "returning", "other", "other"]


# -- case ids --------------------------------------------------------------
def test_extract_case_ids_joiners_typos_and_ranges():
    cl, other = b._extract_case_ids(
        "CL 2021-104102; CL 2021-104106, CL 2021-104358 and CL and CL 2021-104363 & CV 2026-087034"
    )
    assert cl == ["CL 2021-104102", "CL 2021-104106", "CL 2021-104358", "CL 2021-104363"]
    assert other == ["CV 2026-087034"]
    cl, _ = b._extract_case_ids("CL 2019-144850; CL and 2019-144426")
    assert "CL 2019-144426" in cl
    cl, _ = b._extract_case_ids("CL 2015-098835 thru 2015-098838")
    assert cl == ["CL 2015-098835", "CL 2015-098836", "CL 2015-098837", "CL 2015-098838"]
    cl, _ = b._extract_case_ids("CL 2015-098835 thru 2015-098886")   # span too wide -> ends only
    assert cl == ["CL 2015-098835", "CL 2015-098886"]
    cl, other = b._extract_case_ids("OL 2022-000952")
    assert cl == [] and other == ["OL 2022-000952"]


# -- classifier ------------------------------------------------------------
def test_classify_recommendation():
    c = b._classify_recommendation
    assert c("Demolish residential structure") == "demolish"
    assert c("Issue new order for demolition") == "demolish"
    assert c("Issue combined order for demolition of residential and accessory structures") == "demolish"
    assert c("issuing a new order to vacate the property and demolish the structures") == "demolish"
    assert c("Issue order to vacate and secure the structure") == "vacate"
    assert c("Repair Repeat Offender Program commercial multi-family structure") == "repair"
    assert c("Repairs") == "repair"
    assert c("Uphold accrued penalties from Order issued March 24, 2021.") == "uphold_penalties"
    assert c("Deny appeal of the notice of violation.") == "deny_appeal"
    assert c("Pending") == "other"
    assert c("") == "other"


def test_parse_previous_action():
    cls, trv = b._parse_previous_action(
        "Order TRV 2025142639 issued December 9, 2025, for repair of the commercial multi-family structure")
    assert (cls, trv) == ("repair", "TRV 2025142639")
    cls, trv = b._parse_previous_action("BSC Orders TRV 2023061768 and TRV 2023061769, issued May 24, 2023, to repair")
    assert (cls, trv) == ("repair", "TRV 2023061768")
    cls, trv = b._parse_previous_action("Rescinded demolition order issued May 26, 2021; and continued")
    assert (cls, trv) == ("demolish", "")
    assert b._parse_previous_action("Continued from June 23, 2021 meeting") == ("", "")


# -- address / owner -------------------------------------------------------
def test_clean_address():
    assert b._clean_address("9204 Curlew Drive") == ("9204 Curlew Drive", False, "")
    addr, mf, note = b._clean_address("2507 Burleson Road, aka Burl North")
    assert (addr, mf, note) == ("2507 Burleson Road", False, "aka Burl North")
    addr, mf, note = b._clean_address("1817 E Oltorf Street, Building 11 and Fence (aka the Social)")
    assert addr == "1817 E Oltorf Street" and mf and "Building 11" in note
    addr, mf, note = b._clean_address("502 W. Longspur Blvd., Bldg. 4, aka 502 North Apartments")
    assert addr == "502 W Longspur Blvd" and mf
    addr, mf, _ = b._clean_address("6901 Wentworth Drive, Exterior, and Units A, B and D")
    assert addr == "6901 Wentworth Drive" and mf
    addr, mf, _ = b._clean_address("1601 Royal Crest Drive, aka Mia Riverside Apartments")
    assert addr == "1601 Royal Crest Drive" and mf
    addr, _, _ = b._clean_address("2015 and 2017 Payne Ave")
    assert addr == "2015 Payne Ave"
    addr, _, _ = b._clean_address("1619 S. 1st Street aka Jovita's")
    assert addr == "1619 S 1st Street"
    addr, _, _ = b._clean_address("3004 E. 12th Street")
    assert addr == "3004 E 12th Street"
    addr, mf, _ = b._clean_address("8800 N. IH 35, Buildings 1-10 (Exteriors); and Building 10 (Fire Damage)")
    assert addr == "8800 N IH 35" and mf
    addr, _, _ = b._clean_address("2414 S. 1st Street, Unit B")
    assert addr == "2414 S 1st Street"
    addr, mf, note = b._clean_address("1008 Reinli Street (units 112-314)")
    assert addr == "1008 Reinli Street" and mf and "units 112-314" in note
    addr, mf, _ = b._clean_address("2606 Wheless Lane, Office and Buildings 1-9")
    assert addr == "2606 Wheless Lane" and mf
    assert b._split_house_street("9204 Curlew Drive") == ("9204", "Curlew Drive")
    assert b._street_stems("S. 1st Street") == ["S 1ST", "1ST"]
    assert b._street_stems("Curlew Drive") == ["CURLEW"]
    assert b._street_stems("E Cesar Chavez Street") == ["E CESAR", "CESAR"]


def test_parse_owner_strips_parenthetical():
    name, etype, _, _ = b._parse_owner("Bobbie Jean Leare (1704 Miriam Avenue LLC is grantee on TRV 2025131833)")
    assert (name, etype) == ("Bobbie Jean Leare", "")


def test_parse_owner():
    assert b._parse_owner("Thomas A. Rodriguez & Casimiro Rodriguez") == (
        "Thomas A. Rodriguez", "", "", ["Casimiro Rodriguez"])
    assert b._parse_owner("Julian and Carmen Garcia") == ("Julian", "", "", ["Carmen Garcia"])
    name, etype, co, extra = b._parse_owner("Carlos Oviedo c/o Alex Vasquez")
    assert (name, etype, co, extra) == ("Carlos Oviedo", "", "Alex Vasquez", [])
    name, etype, _, _ = b._parse_owner("Brugge 2507 LLC")
    assert (name, etype) == ("Brugge 2507 LLC", "business")
    name, etype, _, _ = b._parse_owner("Longspur Blvd Apts LLC and 4719 Harmon Ave Holding 3 LLC")
    assert etype == "business" and name.startswith("Longspur Blvd Apts LLC")   # never decapitated
    _, etype, _, _ = b._parse_owner("Magnolia Christian Church")
    assert etype == "business"
    name, etype, co, _ = b._parse_owner("St Johns Reg Baptist c/o Rev. G. V. Clark")
    assert (name, etype, co) == ("St Johns Reg Baptist", "business", "Rev. G. V. Clark")
    _, etype, _, _ = b._parse_owner("St Johns Church of Christ Trustees")
    assert etype == "business"
    _, etype, _, _ = b._parse_owner("Doe Family Trust")
    assert etype == "business"
    _, etype, _, _ = b._parse_owner("City of Austin")
    assert etype == "government"
    _, etype, _, _ = b._parse_owner("Austin Independent School District")
    assert etype == "government"
    name, _, _, _ = b._parse_owner("EVELYN MCKINLEY LIFE ESTATE")
    assert name == "Evelyn McKinley"   # ALL-CAPS -> title-cased; suffix stripped
    assert b._parse_owner("Thomas Beckett, Jr.")[0] == "Thomas Beckett, Jr."


def test_reconcile_owner():
    tax, flags = b._reconcile_owner("Thomas A. Rodriguez", "", {"fullname": "RODRIGUEZ TOMAS A &"})
    assert tax == "RODRIGUEZ TOMAS A &" and "owner_mismatch_cad" in flags   # Tomas != Thomas
    tax, flags = b._reconcile_owner("Carlos Oviedo", "", {"fullname": "OVIEDO CARLOS"})
    assert tax == "OVIEDO CARLOS" and flags == []
    tax, flags = b._reconcile_owner("Carlos Oviedo", "", {"fullname": "CARLOS OVIEDO"})
    assert flags == []
    _, flags = b._reconcile_owner("Brugge 2507 LLC", "business", {"fullname": "BRUGGE 2507 LLC"})
    assert flags == []
    _, flags = b._reconcile_owner("Jane Doe", "", {"fullname": "ACME HOLDINGS LLC"})
    assert "owner_entity_mismatch" in flags
    assert b._reconcile_owner("Jane Doe", "", None) == ("", ["cad_no_match"])


# -- full agendas ----------------------------------------------------------
def test_parse_agenda_2026_format_d():
    items, skipped = b.parse_agenda_text(AGENDA_2026)
    by = _by_case(items)
    assert sorted(by) == ["CL 2023-103040", "CL 2025-141760", "CL 2026-076108",
                          "CL 2026-083909", "CL 2026-083918", "CL 2026-090706"]
    assert skipped == 2   # the two CV appeals
    it = by["CL 2026-083909"]
    assert it.section == "new"
    assert it.address_raw == "9204 Curlew Drive"
    assert it.owner_raw == "Thomas A. Rodriguez & Casimiro Rodriguez"
    assert it.rec_class == "repair"
    assert it.recommendation == "Repair residential structure."
    it = by["CL 2026-090706"]
    assert it.owner_raw == "Carlos Oviedo c/o Alex Vasquez"
    it = by["CL 2026-076108"]
    assert it.address_raw == "2507 Burleson Road, aka Burl North"
    assert it.owner_raw == "Brugge 2507 LLC" and it.multifamily
    it = by["CL 2025-141760"]     # returning + escalation to demolish, owner wrapped
    assert it.section == "returning"
    assert it.owner_raw == "Mia Riverside Holdco LLC"
    assert it.rec_class == "demolish"
    assert it.prior_order_class == "repair"
    assert it.trv_number == "TRV 2025142639"
    assert it.address_raw.startswith("1601 Royal Crest Drive")
    it = by["CL 2023-103040"]     # uphold penalties, prior repair
    assert it.rec_class == "uphold_penalties"
    assert it.prior_order_class == "repair" and it.trv_number == "TRV 2023103040"
    assert it.owner_raw == "Hyde Park Collective LLC"


def test_parse_agenda_2022_stray_slashes_and_appeals_first():
    items, skipped = b.parse_agenda_text(AGENDA_2022)
    by = _by_case(items)
    assert sorted(by) == ["CL 2022-117169", "CL 2022-117738", "CL 2022-117754", "CL 2022-118167"]
    assert skipped == 2   # CL deny-appeal + OL license appeal
    assert by["CL 2022-117738"].address_raw == "1908 San Gabriel Street"
    assert by["CL 2022-117169"].rec_class == "demolish"
    it = by["CL 2022-118167"]
    assert it.address_raw.startswith("502 W. Longspur Blvd., Bldg. 4")
    assert it.owner_raw == "Longspur Blvd Apts LLC and 4719 Harmon Ave Holding 3 LLC"
    assert it.repeat_offender and it.multifamily
    assert by["CL 2022-117754"].owner_raw == "Thomas Beckett, Jr."


def test_parse_agenda_2021_unlabeled_address_and_prev_after_rec():
    items, _ = b.parse_agenda_text(AGENDA_2021)
    by = _by_case(items)
    it = by["CL 2017-111475"]
    assert it.section == "returning" and it.rec_class == "demolish"
    assert it.previous_action.startswith("Rescinded demolition order")
    assert it.prior_order_class == "demolish"
    it = by["CL 2021-104102"]
    assert it.case_ids == ["CL 2021-104102", "CL 2021-104106", "CL 2021-104358", "CL 2021-104363"]
    assert it.address_raw == "6901 Wentworth Drive, Exterior, and Units A, B and D"
    assert it.owner_raw == "William and Guisela Morrow"
    it = by["CL 2021-119634"]    # no "Property address" label at all
    assert len(it.case_ids) == 11
    assert it.address_raw.startswith("8800 N. IH 35, Buildings 1-10")
    assert it.owner_raw == "8800 Austin, LLC"
    assert by["CL 2021-085126"].owner_raw == "Magnolia Christian Church"


def test_parse_agenda_2019_format_c():
    items, _ = b.parse_agenda_text(AGENDA_2019)
    by = _by_case(items)
    assert by["CL 2019-163305"].rec_class == "demolish"
    assert by["CL 2019-163305"].address_raw == "1619 S. 1st Street aka Jovita's"
    assert by["CL 2019-163321"].owner_raw == "Julian and Carmen Garcia"
    assert by["CL 2019-166776"].rec_class == "repair"        # "Case Number CL" no colon
    assert by["CL 2019-088180"].owner_raw == "Robert V. Armstrong and Joy Panian Armstrong"
    it = by["CL 2019-144381"]                                # update case, owner on own line
    assert it.section == "update" and it.owner_raw == "CSC North Austin Realty LLC"
    assert "CL 2019-144426" in it.case_ids
    assert it.rec_class == "other"


def test_parse_agenda_2017_format_b():
    items, skipped = b.parse_agenda_text(AGENDA_2017)
    by = _by_case(items)
    assert sorted(by) == ["CL 2017-111451", "CL 2017-111459"]
    assert skipped == 1
    it = by["CL 2017-111459"]
    assert it.address_raw == "5803 Sweeney Circle, Unit D"
    assert it.owner_raw == "Community Partnership for the Homeless, Inc."
    assert it.rec_class == "repair"
    assert by["CL 2017-111451"].owner_raw == "Kennan Properties, LLC"


# -- index page --------------------------------------------------------------
INDEX_HTML = """<html><body>
<div class=bcic_nav>2026 Meetings: Page 1 of 2</div>
<div class=bcic_mtgdate>August 26, 2026</div>
<div class=bcic_mtgtype>Regular Meeting of the Building and Standards Commission</div>
<div class=bcic_doc><a href="https://services.austintexas.gov/edims/document.cfm?id=479428"><img height=18 width=35 alt="pdf icon" src="x.gif" border=0></a><a href="https://services.austintexas.gov/edims/document.cfm?id=479428"><b>Agenda</b></a><small>(117KB)</small></div>
<div class=bcic_doc><a href="https://austintx.new.swagit.com/videos/399300"><b>Play video</b></a></div>
<div class=bcic_mtgdate>July 22, 2026</div>
<div class=bcic_doc><a href="https://services.austintexas.gov/edims/document.cfm?id=476932"><b>Agenda</b></a></div>
<div class=bcic_doc><a href="https://services.austintexas.gov/edims/document.cfm?id=477000"><b>Revised Agenda</b></a></div>
<div class=bcic_doc><a href="https://services.austintexas.gov/edims/document.cfm?id=480082"><b>Approved Minutes</b></a></div>
<div class=bcic_mtgdate>April 22, 2026 (Cancelled)</div>
<div class=bcic_doc><a href="https://services.austintexas.gov/edims/document.cfm?id=470001"><b>Agenda</b></a></div>
<div class=bcic_doc><a href="https://services.austintexas.gov/edims/document.cfm?id=470002"><b>Cancellation Notice</b></a></div>
</body></html>"""


def test_parse_index_page():
    meetings, page, total = b._parse_index_page(INDEX_HTML)
    assert (page, total) == (1, 2)
    assert [m.date for m in meetings] == ["2026-08-26", "2026-07-22", "2026-04-22"]
    assert meetings[0].docs == [("Agenda", 479428)]         # duplicate href deduped, video ignored
    assert b._pick_agenda_doc(meetings[0]) == 479428
    assert b._pick_agenda_doc(meetings[1]) == 477000         # Revised Agenda preferred
    assert meetings[2].cancelled and b._pick_agenda_doc(meetings[2]) is None


def test_get_retries_on_empty_body_then_succeeds():
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, text):
            self.status_code = 200
            self.text = text
            self.content = text.encode()
            self.headers = {}

    class FakeSession:
        def get(self, url, timeout=None):
            calls["n"] += 1
            return FakeResp("" if calls["n"] == 1 else "<div class=bcic_nav>x</div>")

    orig_sleep = b.time.sleep
    b.time.sleep = lambda *_: None
    try:
        resp = b._get(FakeSession(), "http://x", sentinel="bcic_nav")
        assert calls["n"] == 2 and "bcic_nav" in resp.text

        class AlwaysEmpty:
            def get(self, url, timeout=None):
                return FakeResp("")
        try:
            b._get(AlwaysEmpty(), "http://x", sentinel="bcic_nav")
            raise AssertionError("expected failure")
        except b.requests.RequestException:
            pass
    finally:
        b.time.sleep = orig_sleep


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

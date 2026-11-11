"""Owner reconcile + the upload-batch defects it replaces (2026-11-11).

Every case below is a real record the operator hand-patched or held during the
09-18 .. 11-11 upload batches. Offline: county lookups are monkeypatched.

Run: PYTHONPATH=src python -m pytest tests/test_owner_reconcile.py -q
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

import owner_reconcile as orc  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


# ── name matching: debtor IS an owner of record ──────────────────────

MATCH = [
    ("BENAVIDEZ ORIRIS IVETH", "BENAVIDEZ OSIRIS IVETH &", "Osiris", "Benavidez"),   # instrument typo
    ("DIANA SCHAAF", "SCHAAF DIANA", "Diana", "Schaaf"),                              # FIRST-LAST instrument
    ("VAN DELDEN JEFF K", "VAN DELDEN JEFFREY KARAM &", "Jeff", "Van Delden"),        # particle surname
    ("RILEY GERALD STANHOPE", "RILEY GERALD S JR & GINA G", "Gerald", "Riley"),
    ("SOUTHER LACY", "SOUTHER CASEY & LACY", "Lacy", "Souther"),                      # co-owner after &
    ("SERNA ROGELIO", "SERNA FRANCISCO ROGELIO", "Rogelio", "Serna"),                 # goes by middle name
    ("NOBLE LACY DENISE", "Noble, Lacy Denise & Jeffrey Hicks", "Lacy", "Noble"),     # Bell comma form
    ("GUERRIERO JENNIFER MARIE", "Guerriero, Jennifer & Joshua", "Jennifer", "Guerriero"),
    ("MALDONADO RIGOBERTO", "Maldonado, Rigoberto", "Rigoberto", "Maldonado"),
    ("LLOYD EARL JR", "LLOYD KRYSTAL & EARL", "Earl", "Lloyd"),
]

MISMATCH = [
    ("MARTINEZ ELISA", "MARTINEZ ELSA"),                       # near-miss, no corroborating middle
    ("ZAMORA ROLANDA", "ZAMORA ROLANDO & JUANITA S"),
    ("JOHNSON PHILIP B", "JOHNSON LEWIS B"),
    ("ARELLANES PHILLIP JAMES JR", "JAMES DONALD PHILLIP"),
    ("REYES DESTINY C", "Reyes, Juan C Etal"),                 # child vs parent
    ("ROBINSON KENNETH JAMES JR", "Robinson, James"),
    ("SIMMONS BIDELLA M", "SIMMONS STEVE M"),                  # spouse-like
    ("SERRANTO RAYMOND ALEX", "ALEX RAYMOND GAITAN &"),
    ("WILLIAMS MARIA DE LOS MILAGRCS", "WILLIAMS BETTINA ANNA MARIA"),
    ("JONES STEPHEN ADAM", "JONES STEPHEN M TRUST"),           # middle initials disagree
    ("DE ALBA LETICIA", "DE ALBA JUAN"),
    ("BRYANT DAVID A", "Armstrong, Bryant A"),
    ("GUERRERO SUSANA C", "Escobar, Jourdan & Brooke C Guerrero"),
    ("MARSHALL KIPP", "THORNE-WILLIAMS KEITH DEON JR"),         # substitute trustee
]


@pytest.mark.parametrize("debtor,cad,first,last", MATCH)
def test_debtor_matches_owner_of_record(debtor, cad, first, last):
    ok, oriented = orc.person_matches(debtor, cad)
    assert ok, (debtor, cad)
    assert oriented == {"first": first, "last": last}


@pytest.mark.parametrize("debtor,cad", MISMATCH)
def test_debtor_is_not_owner_of_record(debtor, cad):
    assert not orc.person_matches(debtor, cad)[0], (debtor, cad)


def test_entity_matching():
    assert orc.entity_matches("CORDSEN CONSTRUCTION LLC.", "Cordsen Construction Llc")
    assert not orc.entity_matches("SOUTH PARK MALL REALTY LLC", "South Lamar Park Llc")


@pytest.mark.parametrize("cad,disp,entity", [
    ("LANDESS JAMES ALLEN", "James Landess", False),
    ("DE BEST JOHN P & NOEL K", "John De Best", False),        # shared parser gave "Best De"
    ("Gonzalez, Nicasio", "Nicasio Gonzalez", False),
    ("THORNE-WILLIAMS KEITH DEON JR", "Keith Thorne-Williams", False),
    ("VIGIL MAX TRUST THE", "Vigil Max Trust", True),
    ("ATX8221 LLC", "Atx8221 Llc", True),
])
def test_owner_display(cad, disp, entity):
    assert orc.owner_display(cad) == (disp, entity)


def test_strip_glued_city():
    assert orc.strip_glued_city("4611 Outpost Trace Lago Vista", "Lago Vista") == "4611 Outpost Trace"
    assert orc.strip_glued_city("100 Round Rock", "Round Rock") == "100 Round Rock"   # nothing left
    assert orc.strip_glued_city("12 Austin Ave", "Austin") == "12 Austin Ave"


def test_title_address():
    assert orc.title_address("10900 NE 4TH ST") == "10900 NE 4th St"
    assert orc.title_address("PO BOX 40127") == "PO Box 40127"


# ── reconcile_owners (lookups monkeypatched) ─────────────────────────

def _n(**kw):
    base = dict(county="Travis", city="Austin", zip="78701", address="1 Main St")
    base.update(kw)
    return NoticeData(**base)


@pytest.fixture
def cad(monkeypatch):
    table = {}
    monkeypatch.setattr(orc, "lookup_owner_of_record", lambda n: table.get(n.address))
    return table


def test_substitute_trustee_replaced_by_owner_of_record(cad):
    cad["19104 Scoria Dr"] = {"owner_raw": "THORNE-WILLIAMS KEITH DEON JR", "parcel_id": "02746505160000",
                              "mail_street": "19104 SCORIA DR", "mail_city": "PFLUGERVILLE",
                              "mail_state": "TX", "mail_zip": "78660"}
    n = _n(address="19104 Scoria Dr", notice_type="foreclosure",
           owner_name="Kipp Marshall", tax_owner_name="MARSHALL KIPP")
    kept, review = orc.reconcile_owners([n])
    assert not review and kept == [n]
    assert n.owner_name == "Keith Thorne-Williams"
    assert n.tax_owner_name == "THORNE-WILLIAMS KEITH DEON JR"
    assert n.notice_party_name == "MARSHALL KIPP"
    assert (n.owner_street, n.owner_city, n.owner_zip) == ("19104 Scoria Dr", "Pflugerville", "78660")
    assert n.owner_check == "from_cad" and n.parcel_id == "02746505160000"


def test_matching_borrower_kept(cad):
    cad["4529 Tiddle Ln"] = {"owner_raw": "ROSADO JULIO ARMANDO & MELANY O CA"}
    n = _n(address="4529 Tiddle Ln", notice_type="foreclosure", owner_name="Julio Rosado",
           tax_owner_name="ROSADO JULIO ARMANDO & MELANY O CA")
    orc.reconcile_owners([n])
    assert n.owner_name == "Julio Rosado" and n.owner_check == "match" and not n.notice_party_name


def test_ownerless_foreclosure_filled_and_unresolved(cad):
    cad["6309 Emerald Forest Dr"] = {"owner_raw": "VIGIL MAX TRUST THE", "mail_street": "3863 COTTONWOOD GROVE TRL",
                                     "mail_city": "AGOURA HILLS", "mail_state": "CA", "mail_zip": "91301"}
    a = _n(address="6309 Emerald Forest Dr", notice_type="foreclosure")
    b = _n(address="508 Kemp St", notice_type="foreclosure")
    kept, review = orc.reconcile_owners([a, b])
    assert len(kept) == 2 and not review
    assert a.owner_name == "Vigil Max Trust" and a.owner_check == "filled"
    assert b.owner_check == "unresolved"


def test_lien_on_someone_elses_house_held(cad):
    cad["10600 Indigo Broom Loop"] = {"owner_raw": "JOHNSON LEWIS B"}
    n = _n(address="10600 Indigo Broom Loop", notice_type="lien", owner_name="Philip Johnson",
           tax_owner_name="JOHNSON PHILIP B")
    kept, review = orc.reconcile_owners([n])
    assert kept == [] and review == [n]
    assert "not the owner of record" in n.review_reason


def test_lien_without_owner_of_record_held(cad):
    n = _n(address="2519 Halite Bnd", notice_type="lien", owner_name="Justin Leblanc",
           tax_owner_name="LEBLANC JUSTIN")
    kept, review = orc.reconcile_owners([n])
    assert review == [n] and "unverified" in n.review_reason


def test_hospital_debtor_held(cad):
    cad["121 Oakland Rd"] = {"owner_raw": "Taylor, Scott & Tonya"}
    n = _n(address="121 Oakland Rd", county="Williamson", notice_type="lien",
           owner_name="White Scott", tax_owner_name="SCOTT & WHITE HOSPITAL - TAYLOR")
    _kept, review = orc.reconcile_owners([n])
    assert review == [n] and "hospital" in n.review_reason


def test_reversed_lien_name_oriented(cad):
    cad["11502 Brenham St"] = {"owner_raw": "SCHAAF DIANA"}
    n = _n(address="11502 Brenham St", notice_type="lien", owner_name="Schaaf Diana",
           tax_owner_name="DIANA SCHAAF")
    kept, _r = orc.reconcile_owners([n])
    assert kept == [n] and n.owner_name == "Diana Schaaf" and n.owner_check == "match"


def test_name_line_mailing_never_used(cad):
    cad["700 Wayside Dr"] = {"owner_raw": "VAN DELDEN JEFFREY KARAM &", "mail_street": "NATALIE N VAN DELDEN",
                             "mail_city": "AUSTIN", "mail_state": "TX", "mail_zip": "78703"}
    n = _n(address="700 Wayside Dr", notice_type="lien", owner_name="Delden Van",
           tax_owner_name="VAN DELDEN JEFF K")
    orc.reconcile_owners([n])
    assert n.owner_name == "Jeff Van Delden" and n.owner_street == ""


def test_no_house_number_held_and_probate_untouched(cad):
    a = _n(address="Norwood Ln", notice_type="tax_delinquent", owner_name="Janice Milligan")
    b = _n(address="1 Main St", notice_type="probate", owner_name="Clayton Moore")
    kept, review = orc.reconcile_owners([a, b])
    assert review == [a] and kept == [b] and b.owner_check == ""


def test_kill_switch(cad, monkeypatch):
    monkeypatch.setenv("SIFT_SKIP_OWNER_RECONCILE", "1")
    n = _n(address="Norwood Ln", notice_type="lien")
    assert orc.reconcile_owners([n]) == ([n], [])


# ── seal rule: checked-and-ownerless is complete ─────────────────────

def test_unresolved_foreclosure_is_sealable():
    from main import _is_complete_for_seen
    n = _n(address="508 Kemp St", notice_type="foreclosure")
    assert not _is_complete_for_seen(n)
    n.owner_check = "unresolved"
    assert _is_complete_for_seen(n)


# ── Bell owner mailing split ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("165 CEDAR RIDGE CIRKILLEEN, TX 76542", ("165 CEDAR RIDGE CIR", "KILLEEN", "TX", "76542")),
    ("2333 FM 2268SALADO, TX 76571", ("2333 FM 2268", "SALADO", "TX", "76571")),
    ("15041 OLD TROY RDTROY, TX 76579", ("15041 OLD TROY RD", "TROY", "TX", "76579")),
    ("C/O DOLORES LARA806 N 8TH STTEMPLE, TX 76501-2642", ("806 N 8TH ST", "TEMPLE", "TX", "76501")),
    ("AGUSTINA1225 LIBERTY BELL LPKILLEEN, TX 76543-4935", ("1225 LIBERTY BELL LP", "KILLEEN", "TX", "76543")),
    ("733 RATTLESNAKE RDHARKER HEIGHTS, TX 76548-1461", ("733 RATTLESNAKE RD", "HARKER HEIGHTS", "TX", "76548")),
    ("ATTN JOE MAHAN_x000d_\n9450 PRAIRIE VIEW RD_x000d_\nTEMPLE, TX  76502-7036",
     ("9450 PRAIRIE VIEW RD", "TEMPLE", "TX", "76502")),
    ("100 MAIN STSPRINGFIELD, IL 62701", ("100 MAIN ST", "SPRINGFIELD", "IL", "62701")),
    # unknown out-of-state city: split after the street suffix
    ("11720 HENLEY CTWALDORF, MD 20601", ("11720 HENLEY CT", "WALDORF", "MD", "20601")),
    ("5405 CLAIRMONT LNN CHARLESTON, SC 29420", ("5405 CLAIRMONT LN", "N CHARLESTON", "SC", "29420")),
    ("PO BOX 123BELTON, TX 76513", ("PO BOX 123", "BELTON", "TX", "76513")),
])
def test_bell_mailing_split(raw, expected):
    from bell_tax_cache import split_owner_address
    assert split_owner_address(raw, {"SPRINGFIELD"}) == expected


def test_bell_parcel_mailing_city_never_falls_back_to_situs(monkeypatch):
    import bell_tax_cache, cad_lookup
    rec = {"fullname": "SMITH JOHN", "quickrefid": "1", "mailing": "15041 OLD TROY RD",
           "mcity": "TROY", "mstate": "TX", "mzip": "76579", "scity": "TEMPLE", "szip": "76502"}
    monkeypatch.setattr(bell_tax_cache, "search_by_parcel", lambda pid: rec)
    r = cad_lookup._parcel_lookup_one("1", "bell")
    assert (r["mail_city"], r["mail_zip"]) == ("TROY", "76579")
    rec2 = dict(rec, mcity="", mzip="")
    monkeypatch.setattr(bell_tax_cache, "search_by_parcel", lambda pid: rec2)
    r2 = cad_lookup._parcel_lookup_one("1", "bell")
    assert r2["mail_city"] == "" and r2["mail_zip"] == ""


# ── Travis roll: name line in the mailing slot ───────────────────────

def test_travis_pick_mail_line():
    from travis_tax_cache import _pick_mail_line
    assert _pick_mail_line("LUIS ALONSO FRANCO", "11511 MARSHALL ST", "") == "11511 MARSHALL ST"
    assert _pick_mail_line("NATALIE N VAN DELDEN", "", "") == ""
    assert _pick_mail_line("PO BOX 40127", None, None) == "PO BOX 40127"
    assert _pick_mail_line("BOXWOOD LLC", "12 ELM", "") == "12 ELM"


# ── entity rows never get a fabricated person ────────────────────────

def test_entity_name_parse_rejects_digits_and_orients_trusts():
    from entity_researcher import _try_parse_entity_name
    assert _try_parse_entity_name("3423 INVESTMENTS LLC", "llc") is None
    assert _try_parse_entity_name("GUGLER CLARK L REVOCABLE TRUST", "trust")["person_name"] == "Clark L Gugler"
    assert _try_parse_entity_name("JOHN DOE REVOCABLE TRUST", "trust")["person_name"] == "John Doe"


@pytest.mark.parametrize("person,first,last", [
    ("3423", "", ""), ("Jarolik", "", ""), ("Clark L Gugler", "Clark", "Gugler"),
])
def test_formatter_entity_contact_gate(person, first, last):
    from datasift_formatter import _get_contact_info
    n = NoticeData(owner_name="3423 Investments Llc", entity_person_name=person, address="1 Main St")
    c = _get_contact_info(n)
    assert (c["first"], c["last"]) == (first, last)
    assert c["business"] == "3423 Investments Llc"


def test_notice_party_in_notes_and_review_csv(tmp_path, monkeypatch):
    import csv
    import datasift_formatter as df
    n = NoticeData(address="19104 Scoria Dr", owner_name="Keith Thorne-Williams",
                   tax_owner_name="THORNE-WILLIAMS KEITH DEON JR", notice_party_name="MARSHALL KIPP",
                   notice_type="foreclosure", county="Travis", review_reason="debtor X is not the owner")
    sec = df._build_legal_owner_section(n)
    assert "THORNE-WILLIAMS KEITH DEON JR" in sec and "Name on the filing: MARSHALL KIPP" in sec
    monkeypatch.setattr(df, "OUTPUT_DIR", tmp_path)
    info = df.write_review_csv([n])
    rows = list(csv.DictReader(open(info["path"], encoding="utf-8")))
    assert info["count"] == 1 and rows[0]["Review Reason"] == "debtor X is not the owner"
    assert df.write_review_csv([]) is None

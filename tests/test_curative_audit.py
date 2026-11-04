"""Offline tests for src/curative_audit.py (payer classes, names, receipt parse, scoring)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

import curative_audit as ca  # noqa: E402


@pytest.fixture(autouse=True)
def _offline_delinquency(monkeypatch):
    """Scoring tests never read the real county rolls: years come from the record."""
    def fake(rec, cd):
        y = int(rec.get("year_behind_on_taxes") or 0)
        return {"years": y, "first_year": (2025 - y + 1) if y else None, "source": "test" if y else ""}
    monkeypatch.setattr(ca, "delinquency", fake)


def test_to_namelf_strips_markers_and_comma():
    assert ca.to_namelf("HALL, JAMES KEITH & RUTH ANN") == "HALL JAMES KEITH & RUTH ANN"
    assert ca.to_namelf("HALL RUTH ANN (TOD)") == "HALL RUTH ANN"
    assert ca.to_namelf("KASPER, MARSHA JO, EST") == "KASPER MARSHA JO"
    assert ca.to_namelf("ESTATE OF JONES BOB") == "JONES BOB"


def test_person_names_tries_both_coowner_readings():
    names = ca.person_names("HALL, JAMES KEITH & RUTH ANN")
    assert "James Hall" in names and "Ruth Hall" in names
    assert ca.person_names("SELL HOUSE NOW CASH LLC") == []
    assert ca.person_names("* * Request") == []


def test_classify_payer():
    owners = ["HALL RUTH ANN (TOD)"]
    prior = ["HALL, JAMES KEITH & RUTH ANN", "RYAN, WILLIAM D"]
    c = lambda p: ca.classify_payer(p, owners, prior)  # noqa: E731
    assert c("RUTH HALL") == "owner"
    assert c("RUTHHALL") == "same_surname_relative" or c("RUTHHALL") == "owner"
    assert c("JAMES KEITH AND RUTH ANN HALL") == "owner"
    assert c("COTALITY") == "lender_escrow"
    assert c("@UNIVERSITY FEDERAL CREDIT UNION") == "lender_escrow"
    assert c("FIRST AMERICAN REAL ESTATE TAX SERVICE") == "lender_escrow"
    assert c("INDEPENDENCE TITILE") == "title_company"
    assert c("CYNTHIA HALL") == "same_surname_relative"
    assert c("MARIA LOPEZ") == "other_individual"
    assert c("RYAN, WILLIAM D") == "prior_owner"
    assert c("") == "unknown"


def test_travis_paid_by_parse():
    named = ("Process Type: CRC\n...PAYMENT TENDER\nTender Type Tender Reference Paid By\n"
             "CHECK/MONEY ORDER/CASHIERS\nCHK\n9084880 INDEPENDENCE TITILE\nEntity Net Taxable Value")
    assert ca._travis_paid_by(named) == ("CRC", "INDEPENDENCE TITILE")
    lockbox = ("Process Type: RPD\nTender Type Tender Reference Paid By\n8801433 007718\n"
               "Entity Net Taxable Value")
    assert ca._travis_paid_by(lockbox) == ("RPD", "")


def _rec(**kw):
    base = {"county": "Travis", "signals": [], "owner_company": "", "owner_deceased": False,
            "tax_delinquent_value": 0, "estimate_value": 0, "year_behind_on_taxes": 3, "custom_fields": {}}
    base.update(kw)
    return base


def test_assess_drops_title_company_closing_and_entity_buyer():
    cd = {"owner_raw": "SEARIGHT SARAH LEE ALLEN", "deeds": [],
          "payments": [{"date": __import__("datetime").date.today().isoformat(), "amount": 100.0,
                        "payer": "INDEPENDENCE TITILE"}]}
    a = ca.assess(_rec(), cd, {}, [])
    assert a["tier"] == "Drop" and "title company" in a["drops"][0]

    cd = {"owner_raw": "SELL HOUSE NOW CASH LLC",
          "deeds": [{"date": "2026-03-14", "type": "EXECUTOR'S DEED", "grantor": "SHANNON, LEE",
                     "grantee": "SELL HOUSE NOW CASH LLC"}], "payments": []}
    assert ca.assess(_rec(county="Bell"), cd, {}, [])["tier"] == "Drop"


def test_assess_ranks_stuck_estate_with_relative_payer_high():
    cd = {"owner_raw": "CRUMLEY ESTIN EST", "market_value": 400000, "total_due": 4503.0,
          "deeds": [{"date": "1985-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "CRUMLEY ESTIN"}],
          "payments": [{"date": "2019-10-01", "amount": 900.0, "payer": "CYNTHIA CRUMLEY"},
                       {"date": "2018-10-01", "amount": 900.0, "payer": "CYNTHIA CRUMLEY"}]}
    obit = {"owner_deceased": "yes", "date_of_death": "2016-05-01"}
    a = ca.assess(_rec(), cd, obit, [])
    assert a["tier"] == "A", a
    assert a["title"] == "stuck_in_decedent"
    assert any("someone other than the owner" in r for r in a["reasons"])


def test_no_death_evidence_never_tiers_above_c():
    cd = {"owner_raw": "SMITH JOHN", "deeds": [], "market_value": 300000,
          "payments": [{"date": "2018-01-01", "amount": 1.0, "payer": "MARY JONES"}]}
    assert ca.assess(_rec(), cd, {}, [])["tier"] == "C"


# ── chain-of-title defects + ghost payer ─────────────────────────────

def test_dead_by_from_death_certificate_deed():
    deeds = [{"date": "1999-09-10", "type": "DEATH CERTIFICATION", "grantor": "SEARIGHT L ALLEN & CHARLEEN",
              "grantee": "SEARIGHT CHARLEEN"}]
    dead = ca.dead_people(_rec(), {"owner_raw": "SEARIGHT CHARLEEN"}, {}, [], deeds)
    assert [(p["name"], p["dead_by"]) for p in dead] == [("Allen Searight", "1999-09-10")] or \
        any(p["dead_by"] == "1999-09-10" and "Searight" in p["name"] for p in dead)
    assert not any(p["name"] == "Charleen Searight" for p in dead)


def test_ghost_payer_strong_on_travis_card_after_death():
    dead = [{"name": "Estin Crumley", "dead_by": "2016-05-01", "sources": ["obituary"]}]
    pays = [{"date": "2019-10-01", "amount": 900.0, "payer": "ESTIN CRUMLEY", "channel": "CRC", "class": "owner"},
            {"date": "2015-10-01", "amount": 900.0, "payer": "ESTIN CRUMLEY", "channel": "CRC", "class": "owner"}]
    g = ca.ghost_payments(pays, dead, "Travis")
    assert len(g) == 1 and g[0]["date"] == "2019-10-01" and g[0]["strength"].startswith("strong")


def test_ghost_payer_weak_when_williamson_default_owner_account():
    dead = [{"name": "Ruth Hall", "dead_by": "2022-01-01", "sources": ["obituary"]}]
    pays = [{"date": "2023-11-20", "payer": "HALL, JAMES KEITH & RUTH ANN", "payor_id": "547124",
             "owner_party_id": "547124", "class": "owner"},
            {"date": "2023-12-01", "payer": "RUTH HALL", "payor_id": "999", "owner_party_id": "547124",
             "class": "owner"}]
    g = ca.ghost_payments(pays, dead, "Williamson")
    assert [x["strength"].split()[0] for x in g] == ["weak", "strong"]


def test_assess_ghost_payer_behind_ranks_a():
    cd = {"owner_raw": "CRUMLEY ESTIN", "total_due": 4503.0, "market_value": 400000,
          "deeds": [{"date": "1985-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "CRUMLEY ESTIN"}],
          "payments": [{"date": __import__("datetime").date.today().isoformat(), "amount": 300.0,
                        "payer": "ESTIN CRUMLEY", "channel": "CRC"}]}
    a = ca.assess(_rec(), cd, {"owner_deceased": "yes", "date_of_death": "2016-05-01",
                               "decedent_name": "Estin Crumley"}, [])
    assert a["ghosts"] and a["struggling"] and a["tier"] == "A"
    assert any(r.startswith("GHOST PAYER") for r in a["reasons"])


def test_chain_break_and_dead_grantor_and_heir_shares_and_tod():
    dead = [{"name": "John Smith", "dead_by": "2015-01-01", "sources": ["obituary"]}]
    deeds = [  # newest first, as assess passes them
        {"date": "2018-06-01", "type": "WARRANTY DEED", "grantor": "SMITH JOHN", "grantee": "LOPEZ MARIA"},
        {"date": "2010-01-01", "type": "WARRANTY DEED", "grantor": "BROWN TOM", "grantee": "JONES ANN"},
        {"date": "2000-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "SMITH JOHN"},
    ]
    d = ca.chain_defects(deeds, dead, "LOPEZ MARIA")
    assert any(x.startswith("CHAIN BREAK") and "BROWN TOM" in x for x in d)
    assert any(x.startswith("DEAD GRANTOR") for x in d)

    aoh = [{"date": "2025-01-01", "type": "AFFIDAVIT OF HEIRSHIP", "grantor": "SMITH JOHN", "grantee": "SMITH AMY & BOB"}]
    assert any(x.startswith("HEIRS HOLD SHARES") for x in ca.chain_defects(aoh, dead, "SMITH JOHN"))

    tod = [{"date": "2012-01-01", "type": "TRANSFER ON DEATH DEED", "grantor": "SMITH JOHN", "grantee": "SMITH AMY"}]
    assert any(x.startswith("TOD NOT COMPLETED") for x in ca.chain_defects(tod, dead, "SMITH JOHN"))

    # Executor / trustee deeds legitimately change the signer - no chain break.
    ok = [{"date": "2018-01-01", "type": "EXECUTOR'S DEED", "grantor": "SHANNON LEE", "grantee": "DOE JANE"},
          {"date": "2000-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "SMITH JOHN"}]
    assert not any(x.startswith("CHAIN BREAK") for x in ca.chain_defects(ok, [], "DOE JANE"))


def test_estate_of_segment_is_first_last_and_life_estate_is_not_death():
    names = ca.person_names("WOODS BETTY J & ESTATE OF MAURICE LEE WOODS")
    assert "Maurice Lee Woods" in names and "Betty Woods" in " ".join(names) or "Betty J Woods" in names
    assert ca.person_names("ESTATE OF WILLIAM ARTHUR MASON") == ["William Arthur Mason"]
    assert not ca.ESTATE_RE.search("DELISLE CHARLES WAYNE LIFE ESTATE")
    assert ca.ESTATE_RE.search("GARCIA PETRA C ESTATE")
    assert ca.is_entity("AUSTERRA STABLE INCOME FUND LP ETAL")


def test_estate_payer_is_not_a_ghost_and_spouses_are_not_fractional():
    dead = [{"name": "Marsha Jo Kasper", "dead_by": "2020-07-12", "sources": ["probate"]}]
    pays = [{"date": "2026-07-02", "payer": "KASPER MARSHA JO Est (O0953045)", "class": "owner"}]
    assert ca.ghost_payments(pays, dead, "Williamson") == []
    assert not any("FRACTIONAL" in d for d in ca.chain_defects([], [], "MCDONALD GEORGE M & ELLA MAE"))
    assert any("FRACTIONAL" in d for d in ca.chain_defects([], [], "GOMEZ ROSENDO JR ETAL"))


def test_same_day_deed_order_is_not_a_chain_break():
    deeds = [{"date": "2016-02-18", "type": "WARRANTY DEED", "grantor": "KING JAMES", "grantee": "GREEN BILLY"},
             {"date": "2016-02-18", "type": "WARRANTY DEED", "grantor": "SMITH AL", "grantee": "KING JAMES"},
             {"date": "2001-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "SMITH AL"}]
    assert not any(d.startswith("CHAIN BREAK") for d in ca.chain_defects(deeds, [], "GREEN BILLY"))


def test_estate_segments_in_prose_order_parse_first_last():
    seg = ca.owner_segments
    assert seg("MONTES JOSE & ESTATE OFJUANITA MONTES")[0] == ("Juanita Montes", True)
    assert seg("LOPEZ MARGARITA (LE) & THE WALLACE LOPEZ ESTATE")[0] == ("Wallace Lopez", True)
    assert ("Thomas Edward Fulcomer", True) in seg("BREWER, ANGEL & THOMAS EDWARD FULCOMER ESTATE")
    assert seg("BARBOSA YOLANDA ESTATE OF") == [("Yolanda Barbosa", True)]
    assert ("Nat Lopez", True) in seg("LOPEZ, THE ESTATE OF NAT & WALLACE LOPEZ EST")


def test_name_match_middle_and_suffix():
    assert ca.name_match("James D. Hall, II", "James Keith Hall") == "no"
    assert ca.name_match("Mary Jean Kelley", "Mary F Kelley") == "no"
    assert ca.name_match("Henry S. Garza", "Henry S Garza") == "strong"
    assert ca.name_match("Jim Thomas, Jr.", "Jim Jr Thomas") == "strong"
    assert ca.name_match("Linda M. Bates", "Linda Bates") == "weak"


def test_probate_name_hits_are_graded_not_trusted():
    cd = {"owner_raw": "LOPEZ MARGARITA (LE) & THE WALLACE LOPEZ ESTATE"}
    deeds = [{"date": "2013-04-22", "type": "", "grantor": "X", "grantee": cd["owner_raw"]}]
    cases = [{"case_number": "12-0438-CP4", "style": "Estate Of Margarita Lopez, Deceased",
              "date_of_death": "2012-04-22", "filed": "2012-06-13"},
             {"case_number": "18-1501-C425", "style": "Svetlik vs. Reyna and James A. Hall"},
             {"case_number": "20-0001-CP4", "style": "Estate Of Wallace Lopez, Deceased", "filed": "2010-01-01"}]
    acc, poss, out = ca.grade_probate(_rec(county="Williamson"), cd, {}, cases, deeds)
    assert [c["case_number"] for c in acc] == ["20-0001-CP4"]          # estate label corroborates
    assert any("took title 2013-04-22" in r for r in out)               # Margarita bought after "dying"
    assert any("not an estate case" in r for r in out)
    assert poss == []


def test_obituary_contradicted_by_later_purchase_is_ruled_out():
    cd = {"owner_raw": "MONTES JOSE & ESTATE OF JUANITA MONTES", "deeds": [
        {"date": "2020-12-30", "type": "", "grantor": "X", "grantee": "MONTES JOSE & JUANITA"},
        {"date": "2007-03-29", "type": "", "grantor": "Y", "grantee": "MONTES JOSE & ESTATE OF JUANITA MONTES"}],
        "payments": []}
    a = ca.assess(_rec(county="Williamson"), cd, {"owner_deceased": "yes", "date_of_death": "2007-03-27",
                                                  "searched_name": "MONTES JOSE"}, [])
    assert any(r.startswith("obituary ruled out") for r in a["ruled_out"])
    assert not any(p["name"] == "Jose Montes" for p in a["dead"])
    # Juanita: the estate deed proves death by 2007, yet a 2020 deed names her
    assert any(d.startswith("DEED TO A DEAD PERSON") and "Juanita" in d for d in a["defects"])


def test_spouse_obituary_is_a_household_death_not_the_owner():
    cd = {"owner_raw": "MEDLOCK LEE HURLEY", "deeds": [], "payments": []}
    a = ca.assess(_rec(county="Williamson"), cd, {"owner_deceased": "yes", "date_of_death": "2019-01-01",
                                                  "decedent_name": "Dorothy L Medlock"}, [])
    assert any("household death" in r for r in a["ruled_out"])
    assert a["tier"] == "C" and not a["dead"]


def test_ghost_exceptions_junior_estate_runtogether_and_joint_account():
    dead = [{"name": "John C Mercado", "dead_by": "2023-12-30", "sources": ["obituary"]},
            {"name": "Livie Gault Jr", "dead_by": "2009-12-17", "sources": ["probate"]},
            {"name": "Lee Hurley Medlock", "dead_by": "2015-06-08", "sources": ["obituary"]}]
    pays = [{"date": "2024-10-23", "payer": "JOHN C MERCADO JR", "payor_id": "1", "owner_party_id": "2"},
            {"date": "2015-01-19", "payer": "LIVIE JR ESTGAULT,", "payor_id": "1", "owner_party_id": "2"},
            {"date": "2016-07-15", "payer": "MEDLOCK, LEE HURLEY & DOROTHY L", "payor_id": "1", "owner_party_id": "2"}]
    g = ca.ghost_payments(pays, dead, "Williamson", living=["Dorothy L Medlock"])
    assert [x["payer"] for x in g] == ["MEDLOCK, LEE HURLEY & DOROTHY L"]
    assert g[0]["strength"].startswith("weak (joint account")


def test_conveyed_out_after_death_drops_and_truncated_grantee_is_not_a_dead_grantee():
    cd = {"owner_raw": "ESTATE OF WILLIAM ARTHUR MASON", "payments": [], "deeds": [
        {"date": "2023-08-24", "type": "", "grantor": "MASON, WILLIAM ARTHUR", "grantee": "RYCHETSKY REGAN J & PENNY V"},
        {"date": "2022-10-08", "type": "", "grantor": "MASON, WILLIAM ARTHUR",
         "grantee": "ESTATE OF WILLIAM ARTHUR MASON"}]}
    a = ca.assess(_rec(county="Williamson"), cd, {}, [])
    assert a["tier"] == "Drop" and "RYCHETSKY" in a["drops"][0]
    dead = [{"name": "Wallace Lopez", "dead_by": "2002-01-02", "sources": ["deed"]}]
    deeds = [{"date": "2013-04-22", "type": "", "grantor": "X", "grantee": "LOPEZ MARGARITA (LE) & THE WALLACE"}]
    assert ca.took_title_after("Wallace Lopez", deeds, "2002-01-02") is None
    assert not any("DEAD PERSON" in d for d in ca.chain_defects(deeds, dead, "LOPEZ MARGARITA"))


# ── Jordan John triage: years behind, spread, birth year, memorials, docket ──

def test_years_sweet_spot_scores_and_thin_spread_caps_tier():
    base = {"owner_raw": "CRUMLEY ESTIN EST", "total_due": 4503.0,
            "deeds": [{"date": "1985-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "CRUMLEY ESTIN"}],
            "payments": [{"date": "2019-10-01", "amount": 900.0, "payer": "CYNTHIA CRUMLEY"}]}
    obit = {"owner_deceased": "yes", "date_of_death": "2016-05-01"}
    rich = ca.assess(_rec(year_behind_on_taxes=3), {**base, "market_value": 400000}, obit, [])
    assert rich["tier"] == "A" and rich["spread"] == round(400000 * 0.6 - 4503)
    assert any("sweet spot" in r for r in rich["reasons"])
    thin = ca.assess(_rec(year_behind_on_taxes=3), {**base, "market_value": 120000}, obit, [])
    assert thin["gate"].startswith("1 - FAIL") and thin["tier"] == "C"   # 72,000 - 4,503 < 75K
    assert rich["gate"].startswith("2 - PENDING")   # problem found, clerk liens not searched yet
    old = ca.assess(_rec(year_behind_on_taxes=7), {**base, "market_value": 400000}, obit, [])
    assert any("heir-occupied" in r for r in old["reasons"])


def test_delinquency_counts_bell_years_on_one_yardstick(monkeypatch):
    monkeypatch.undo()   # the real function, Bell needs no roll file
    monkeypatch.setattr(ca, "_latest_delq_year", lambda: 2025)
    cd = {"tax_years": [{"year": 2026, "balance": 900, "delinquent": False},
                        {"year": 2025, "balance": 800, "delinquent": True},
                        {"year": 2024, "balance": 700, "delinquent": True},
                        {"year": 2023, "balance": 0, "delinquent": False}]}
    d = ca.delinquency({"county": "Bell"}, cd)
    assert d == {"years": 2, "first_year": 2024, "source": "Bell GSA per-year balances"}


FAG_HTML = """
<div class="memorial-item px-2 row" id="sr-1">
 <a class="d-flex" href="/memorial/111/rufino-ortiz" data-scroll-offset="1">
 <h2 class="name-grave d-flex"><i class="pe-2 text-break">Rufino Ortiz</i></i></h2>
 <b class="birthDeathDates fw-light">16 Nov 1902 &ndash; 8 Oct 1974</b></a>
 <p class="addr-cemet mb-1">Aibonito, Puerto Rico</p></div>
<div class="memorial-item px-2 row" id="sr-2">
 <a class="d-flex" href="/memorial/222/rufino-ortiz" data-scroll-offset="1">
 <h2 class="name-grave d-flex"><i class="pe-2 text-break">Rufino G Ortiz Jr</i></i></h2>
 <b class="birthDeathDates fw-light">1941 &ndash; 3 Mar 2019</b></a>
 <p class="addr-cemet mb-1">Taylor, Williamson County, Texas, USA</p></div>
"""


def test_parse_findagrave_cards():
    m = ca.parse_findagrave(FAG_HTML)
    assert [x["id"] for x in m] == ["111", "222"]
    assert m[1] == {"id": "222", "name": "Rufino G Ortiz Jr", "birth_year": 1941, "death_year": 2019,
                    "death_date": "2019-03-03", "place": "Taylor, Williamson County, Texas, USA"}


def test_obituary_birth_year_patterns():
    assert ca.obituary_birth_year("He was born on May 17, 1950 in Pulaski, Virginia.") == 1950
    assert ca.obituary_birth_year("John Smith (1931 - 2019) of Taylor", "2019-04-01") == 1931
    assert ca.obituary_birth_year("John Smith, 74, of Lecanto passed away", "2022-02-01") == 1948
    assert ca.obituary_birth_year("nothing useful", "2022-02-01") is None


def test_obituary_with_wrong_birth_year_is_ruled_out():
    cd = {"owner_raw": "SMITH JOHN", "market_value": 400000, "total_due": 5000.0, "payments": [],
          "deeds": [{"date": "1990-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "SMITH JOHN"}]}
    rec = _rec(uuid="u1", owner_first="John", owner_last="Smith",
               owner_age=__import__("datetime").date.today().year - 1960)
    obit = {"owner_deceased": "yes", "date_of_death": "2020-01-01",
            "obituary_url": "https://x.com/john-smith-obituary"}
    a = ca.assess(rec, cd, obit, [], {"obit_birth": {"u1": {"birth_year": 1931}}})
    assert a["tier"] == "C" and any("birth year of 1931" in r for r in a["ruled_out"])
    ok = ca.assess(rec, cd, obit, [], {"obit_birth": {"u1": {"birth_year": 1960}}})
    assert any("matches the owner's age" in r for r in ok["reasons"]) and ok["death_proof"] == "obituary only"


def test_findagrave_memorial_is_death_evidence_only_when_it_fits():
    deeds = [{"date": "1995-06-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "ORTIZ RUFINO G JR"}]
    cd = {"owner_raw": "ORTIZ RUFINO G JR", "market_value": 300000, "total_due": 9000.0, "payments": [],
          "deeds": deeds}
    verify = {"findagrave": {"rufino g ortiz jr": ca.parse_findagrave(FAG_HTML),
                             "rufino ortiz jr": ca.parse_findagrave(FAG_HTML)}}
    a = ca.assess(_rec(county="Williamson"), cd, {}, [], verify)
    assert a["memorials"] and a["memorials"][0]["id"] == "222"
    assert a["death_proof"] == "memorial only"
    assert any(p["dead_by"] == "2019-03-03" for p in a["dead"])
    # a memorial that died before the person bought the house is a namesake
    early = [{"date": "2021-01-01", "type": "WARRANTY DEED", "grantor": "X", "grantee": "ORTIZ RUFINO G JR"}]
    b = ca.assess(_rec(county="Williamson"), {**cd, "deeds": early}, {}, [], verify)
    assert not b["memorials"]


def test_docket_divorce_between_coowners_and_open_tax_suit():
    deeds = [{"date": "1990-01-01", "type": "", "grantor": "X", "grantee": "GARZA ALBERT & MARIA GUADALUPE"}]
    cd = {"owner_raw": "GARZA ALBERT & MARIA GUADALUPE", "deeds": deeds, "payments": []}
    other = [{"case_number": "95-399-F368", "filed": "1995-03-24", "type_status": "Divorce (no children) / Disposed",
              "style": "In The Matter Of The Marriage Of Maria Guadalupe Garza And Albert Garza"},
             {"case_number": "24-001-T277", "filed": "2024-02-01", "type_status": "Tax Case - Real Property / Pending",
              "style": "Taylor ISD vs. Albert Garza, Et Al"},
             {"case_number": "88-001-T277", "filed": "1988-02-01", "type_status": "Tax Case - Real Property / Disposed",
              "style": "Taylor ISD vs. Albert Garza"}]
    a = ca.assess(_rec(county="Williamson"), cd, {}, [], None, other)
    assert any(d.startswith("DIVORCE BETWEEN CO-OWNERS") for d in a["defects"])
    assert not any("OPEN TAX SUIT" in r for r in a["reasons"])   # first+last only: a note, not points
    assert any("24-001-T277" in n for n in a["court_notes"])
    assert not any("88-001-T277" in n for n in a["court_notes"])   # filed before they owned it


# ── Ancestry (logged-in) results ─────────────────────────────────────

def test_pick_death_record_gates_on_birth_year_and_title_date():
    import ancestry_enricher as anc
    recs = [{"category": "Death, Burial, Cemetery & Obituaries", "collection": "U.S., Social Security Death Index",
             "name": "Jerry C Alley", "birth_year": 1931, "death_year": 2001, "residence": "Texas", "death": "2001"},
            {"category": "Death, Burial, Cemetery & Obituaries", "collection": "Texas, Death Index",
             "name": "Jerry C Alley", "birth_year": 1950, "death_year": 2009, "residence": "", "death": "2009"},
            {"category": "Death, Burial, Cemetery & Obituaries", "collection": "Texas, Death Index",
             "name": "Jerry C Alley", "birth_year": 1950, "death_year": 2015, "residence": "Kaufman, Texas",
             "death": "12 Jan 2015"}]
    best = anc.pick_death_record(recs, "Jerry", "Alley", "C", 1950, alive_after="2011-01-01")
    assert best["death_year"] == 2015          # 1931 = wrong man, 2009 = before he bought
    assert anc.pick_death_record(recs[:1], "Jerry", "Alley", "", None) is None   # no birth year, no middle


def test_ancestry_death_index_is_death_proof():
    deeds = [{"date": "2011-03-01", "type": "WARRANTY DEED", "grantor": "HUD", "grantee": "ALLEY JERRY C"}]
    cd = {"owner_raw": "ALLEY JERRY C", "market_value": 300000, "total_due": 12000.0, "payments": [], "deeds": deeds}
    verify = {"ancestry": {"jerry c alley": {"birth_year_hint": 1950, "death": {
        "collection": "Texas, U.S., Death Index", "name": "Jerry C Alley", "death": "12 Jan 2015",
        "death_year": 2015, "birth_year": 1950, "url": "https://www.ancestry.com/x"},
        "census": [{"name": "Charles L Alley", "birth_year": 1948, "birthplace": "Virginia", "relation": "Son",
                    "home_1950": "Pulaski, Virginia", "relatives": ["Lossie", "Stella"]}]}}}
    a = ca.assess(_rec(), cd, {}, [], verify)
    assert a["death_proof"] == "death index (SSDI / state)"
    assert any(p["dead_by"] == "2015-01-12" for p in a["dead"])
    assert a["census_leads"] and "verify" in a["census_leads"][0]


def test_child_death_and_foreign_collection_are_namesakes():
    import ancestry_enricher as anc
    base = {"category": "Death, Burial, Cemetery & Obituaries", "name": "Walter R Jasper", "residence": "",
            "birth_year": 1950, "death_year": 2005, "death": "2005"}
    assert anc.pick_death_record([{**base, "collection": "Germany, Find a Grave Index, 1600s-Current"}],
                                 "Walter", "Jasper", "R", 1950) is None
    assert anc.pick_death_record([{**base, "collection": "U.S., Find a Grave Index, 1600s-Current"}],
                                 "Walter", "Jasper", "R", 1950)
    assert anc.pick_death_record([{**base, "collection": "Texas, Death Index", "death_year": 1955}],
                                 "Walter", "Jasper", "R", 1950) is None
    deeds = [{"date": "1990-01-01", "type": "", "grantor": "X", "grantee": "LEDESMA MARIA"}]
    cd = {"owner_raw": "LEDESMA MARIA", "deeds": deeds, "payments": []}
    infant = [{"id": "9", "name": "Maria Ledesma", "birth_year": 1949, "death_year": 1949,
               "death_date": "1949-05-23", "place": "Austin, Travis County, Texas"}]
    rec = _rec(owner_first="Maria", owner_last="Ledesma", owner_age=__import__("datetime").date.today().year - 1951)
    assert ca.memorial_deaths(rec, cd, deeds, {"findagrave": {"maria ledesma": infant}})[0] == []



# ── Gate 1 / arrears / clerk encumbrances ────────────────────────────

def test_gate1_uses_all_known_arrears_not_just_taxes():
    rec = _rec(estimate_value="300000", equity_percent="40", foreclosure_date="2026-09-01", lists=["Foreclosure"])
    g = ca.gate1(rec, {"market": 300000, "taxes_due": 5000.0, "source": "test"})
    # 60% of 300K = 180K; minus 5K taxes and 180K est. mortgage (60% of 300K owed)
    assert g["spread"] == 180000 - 5000 - 180000 and not g["pass"]
    assert any("in foreclosure" in w for w in g["arrears"]["warnings"])
    free = ca.gate1(_rec(estimate_value="300000", equity_percent="100"), {"market": 300000, "taxes_due": 5000.0})
    assert free["pass"] and free["spread"] == 175000
    assert not ca.gate1(_rec(), {"market": None, "taxes_due": 0})["pass"]


def test_unknown_mortgage_is_listed_not_guessed():
    a = ca.arrears(_rec(), {"taxes_due": 1000.0})
    assert a["known_total"] == 1000.0 and any("mortgage unknown" in i["label"] for i in a["unknown"])


def test_clerk_parsers_and_encumbrances():
    import clerk_records as cr
    T, N = chr(9), chr(10)
    recs = [("1", "2015012345", "03/02/2015", "DEED OF TRUST", "SMITH JOHN A", "WELLS FARGO BANK NA"),
            ("2", "2016099999", "05/05/2019", "ABSTRACT OF JUDGMENT", "CAPITAL ONE BANK", "SMITH JOHN A"),
            ("3", "2020000001", "01/10/2020", "RELEASE", "WELLS FARGO BANK NA", "SMITH JOHN A"),
            ("4", "2025000002", "06/10/2025", "NOTICE OF SUBSTITUTE TRUSTEE SALE", "SMITH JOHN A", "X"),
            ("5", "2017000003", "06/10/2017", "DEED OF TRUST", "SMITH JOHN Q", "OTHER BANK")]
    grid = N.join(f"{n}{T}{T}{T}{i}{N}{d}{T}{t}{T}[R] {r}{N}[E] {e}" for n, i, d, t, r, e in recs)
    rows = cr.parse_tcc_grid(grid)
    assert [r["instrument"] for r in rows] == ["2015012345", "2016099999", "2020000001", "2025000002", "2017000003"]
    encs = cr.encumbrances(rows, "John A Smith", since="2010-01-01")
    by = {e["instrument"]: e for e in encs}
    assert "2017000003" not in by                      # John Q Smith is a different man
    assert by["2015012345"]["released"]                # Wells Fargo released it in 2020
    assert not by["2016099999"]["released"]            # the judgment is still open
    open_ = {e["instrument"] for e in cr.open_items(encs)}
    assert open_ == {"2016099999", "2025000002"}
    assert cr.doc_class("RELEASE OF LIEN") == "release" and cr.doc_class("HOME EQUITY LOAN") == "mortgage"
    assert cr.doc_class("FEDERAL TAX LIENS AND NOTICES") == "tax_lien"
    govos = cr.parse_govos_rows([["", "", "", "SMITH JOHN A", "WELLS FARGO", "DEED OF TRUST", "3/14/2002",
                                  "2002009780", "OPR/4633/198", "Property Description: LOT 4 BLK 2"]])
    assert govos[0]["date"] == "2002-03-14" and govos[0]["legal"] == "LOT 4 BLK 2"


def test_top_opportunities_orders_gates_and_dedupes_owner():
    def row(gate, tier, score, owner, street="1 A St"):
        return ({"street": street}, {"gate": gate, "tier": tier, "score": score, "owner_raw": owner, "spread": 1})
    rows = [row("2 - PENDING: clerk", "A", 99, "SMITH JOHN"),
            row("3 - DEEP RESEARCH", "B", 10, "DOE JANE"),
            row("3 - DEEP RESEARCH", "A", 50, "ROE RICK"),
            row("3 - DEEP RESEARCH", "A", 40, "ROE RICK", "2 B St"),
            row("3 - DEEP RESEARCH", "C", 90, "LOW LARRY"),
            row("2 - FAIL: no curative problem found", "A", 95, "NOPE NED")]
    top = ca.top_opportunities(rows, 10)
    assert [t[1]["owner_raw"] for t in top] == ["ROE RICK", "SMITH JOHN", "DOE JANE"]   # tier, then gate


def _brief_a(**kw):
    a = {"tier": "A", "score": 60, "gate": "3 - DEEP RESEARCH", "owner_raw": "DOE JOHN Q", "deeds": [],
         "dead": [], "probate": [], "possible_probate": [], "defects": [], "clerk_open": [], "clerk_notes": [],
         "court_notes": [], "reasons": [], "ghosts": [], "struggling": False, "heirs": [], "spread": 200_000,
         "market_value": 400_000, "arrears": {"items": [], "known_total": 5_000, "unknown": [], "warnings": []},
         "title": "stuck_in_decedent", "death_proof": "court/recorded", "years_delinquent": 3, "total_due": 5_000,
         "last_payment": ""}
    a.update(kw)
    return a


def test_six_defects_and_offer_for_an_orphaned_estate_in_tax_suit():
    rec = {"street": "1 Main St", "owner_mailing": {"street": "9 Elm", "city": "Reno", "state": "NV"}}
    a = _brief_a(dead=[{"name": "John Q Doe", "dead_by": "2019-01-01", "sources": ["probate"]}],
                 defects=["CHAIN BREAK: x", "FRACTIONAL OWNERS on the roll (x) - all must sign"],
                 reasons=["OPEN TAX SUIT 123 filed 2025-01-01 - the county is foreclosing; the clock is running"],
                 clerk_open=[{"doc_type": "ABSTRACT OF JUDGMENT", "date": "2020-01-01", "creditor": "BANK",
                              "instrument": "99"}])
    d = ca.six_defects(rec, a)
    assert set(d) == {"Orphaned estate", "Break in chain", "Feuding owners", "Unreleased lien / judgment",
                      "Missing owner"}
    offers = ca.pick_offer(rec, a, d)
    assert offers[0] == "Net cash offer" and "Heir buyouts (deed + promissory note)" in offers
    text = "\n".join(ca.deal_brief(rec, a, {}, top_rank=3))
    assert "TOP 100 #3" in text and "4+ years since death" in text and "FIX (Orphaned estate)" in text


def test_living_owner_with_big_equity_and_no_defect_gets_a_jv():
    rec = {"street": "1 Main St", "owner_mailing": {"street": "1 Main St", "city": "Austin", "state": "TX"}}
    a = _brief_a(title="recent_transfer", struggling=True)
    d = ca.six_defects(rec, a)
    assert d == {}
    assert ca.pick_offer(rec, a, d)[0] == "Joint venture"


def test_hispanic_and_comma_style_names():
    people = lambda raw: ca.county_people({}, {"owner_raw": raw}, [])   # noqa: E731
    assert people("GARCIA, PEDRO & MATILDE G LAVENDER")[:3] == ["Pedro Garcia", "Matilde G Lavender",
                                                                 "Matilde G Garcia"]
    assert "Maria Del Carmen Garcia Lopez" in people("GARCIA LOPEZ MARIA DEL CARMEN")
    assert "Maria Del Carmen Garcia" in people("GARCIA LOPEZ MARIA DEL CARMEN")
    assert people("DE LA CRUZ JUAN & MARIA") == ["Juan De La Cruz", "Maria De La Cruz"]
    assert "Ruth Hall" in people("HALL JAMES KEITH & RUTH ANN")
    assert ca.name_match("Maria Del Carmen Garcia Lopez", "Maria Garcia") == "weak"
    assert ca.name_match("Ma Guadalupe Rodriguez", "Maria G Rodriguez") == "strong"
    assert ca.name_match("Juan De La Cruz", "Juan Cruz") == "weak"
    assert ca.name_match("Maria E Garcia", "Maria A Garcia") == "no"
    assert ca.name_match("Pedro Garcia", "Matilde Garcia") == "no"


def test_and_joined_and_prose_co_owners():
    from curative_audit import person_names
    assert person_names("MCLAUGHLIN SHANE & MENDY and HERMAN & THELMA A MCGARRAH") == [
        "Shane Mclaughlin", "Mendy Mclaughlin", "Herman Mclaughlin", "Thelma A Mcgarrah"]
    assert person_names("SMITH AND SONS LLC") == []


def test_survivor_holds_title_is_not_a_title_problem():
    from curative_audit import survivor_holds_title
    deeds = [{"date": "2010-06-01", "type": "DEATH CERTIFICATION", "grantor": "LYNN ALVIN & MARY", "grantee": "LYNN MARY"},
             {"date": "1997-07-24", "type": "WARRANTY DEED", "grantor": "BUILDER", "grantee": "LYNN ALVIN & MARY"}]
    assert survivor_holds_title(deeds, "LYNN MARY")["grantee"] == "LYNN MARY"
    assert survivor_holds_title(deeds, "LYNN ALVIN & MARY") is None        # roll still lists the dead spouse
    assert survivor_holds_title(deeds, "LYNN MARY ESTATE") is None         # the survivor died too


def test_recent_grantee_who_sold_out_is_off_title():
    from curative_audit import recent_grantees
    deeds = [{"date": "2024-07-26", "grantor": "ELGIN DONALD R TRUSTEE OF DONALD R", "grantee": "TRAFTON WILLIAM F"},
             {"date": "2016-04-05", "grantor": "ELGIN, DONALD R", "grantee": "ELGIN DONALD R TRUSTEE OF DONALD R"}]
    assert recent_grantees(deeds) == ["TRAFTON WILLIAM F"]


def test_administrator_deed_to_owner_is_cured():
    from curative_audit import survivor_holds_title
    deeds = [{"date": "2024-10-25", "type": "ADMINISTRATOR DEED", "grantor": "GILBERT LYNN", "grantee": "GILBERT JASON ALAN"},
             {"date": "2002-11-26", "type": "DEATH CERTIFICATION", "grantor": "GILBERT JAMES W", "grantee": "GILBERT LYNN"}]
    assert survivor_holds_title(deeds, "GILBERT JASON ALAN")
    crofford = [{"date": "2008-04-12", "type": "DEATH CERTIFICATION", "grantor": "CROFFORD IRA JR", "grantee": "CROFFORD MARIAN"}]
    assert survivor_holds_title(crofford, "CROFFORD MARIAN") is None   # a death certificate is not a conveyance


def test_memorial_conflicting_with_a_fuller_reading_is_a_namesake():
    from curative_audit import memorial_deaths
    rec = {"owner_first": "Rogelio", "owner_last": "Aguilar", "owner_age": 81, "county": "Travis"}
    cd = {"owner_raw": "AGUILAR ROGELIO CRUZ"}
    mem = {"id": "1", "name": "Rogelio Vallejo Aguilar", "death_date": "2014-06-14", "birth_year": 1945,
           "death_year": 2014, "place": "Austin, Travis County, Texas"}
    verify = {"findagrave": {"rogelio aguilar": [mem], "rogelio cruz aguilar": [mem]}}
    accepted, _ = memorial_deaths(rec, cd, [], verify)
    assert accepted == []


def test_signed_after_finds_a_living_namesake(tmp_path, monkeypatch):
    import json
    import clerk_records as cr
    monkeypatch.setattr(cr, "CACHE", tmp_path)
    (tmp_path / "Williamson_Sammy_L_Watts_all.json").write_text(json.dumps({"rows": [
        {"date": "2017-06-21", "doc_type": "DEED", "grantors": ["WATTS SAMMY L"], "grantees": ["X"]}]}))
    assert cr.signed_after("Williamson", "Sammy L Watts", "1991-04-02")["date"] == "2017-06-21"
    assert cr.signed_after("Williamson", "Sammy L Watts", "2020-01-01") is None


def test_trust_words_are_not_a_person():
    # 'VEDDER FAMILY TRUST' read as 'Family Vedder' sent junk names to Find A Grave
    assert ca.person_names("VEDDER FAMILY TRUST") == []
    assert ca.person_names("PEREZ ADOLFO LIVING TRUST") == ["Adolfo Perez"]
    assert "Allan B Forte" in ca.person_names("FORTE ALLAN B & VILMA L TRUSTEES")
    assert ca.person_names("TRUSTY JOHN") == ["John Trusty"]


def test_old_will_settles_the_death_even_with_later_deeds():
    # Boriskie: Robert O's share passed by will in 1996; Barry signed a deed in 2013 - alive, cured
    deeds = [
        {"date": "2013-06-07", "type": "SPECIAL WARRANTY DEED", "grantor": "BORISKIE BARRY W &", "grantee": "BORISKIE BARRY W"},
        {"date": "1996-04-12", "type": "WILL TO SURVIVING SPOUSE ONLY", "grantor": "BORISKIE BARRY W & ROBERT O",
         "grantee": "BORISKIE BARRY W & NORMA"},
        {"date": "1991-04-08", "type": "SPECIAL WARRANTY DEED", "grantor": "FDIC", "grantee": "BORISKIE BARRY W & ROBERT O"},
    ]
    assert ca.survivor_holds_title(deeds, "BORISKIE BARRY W")["date"] == "1996-04-12"
    # a plain death certificate that keeps the same people is NOT a cure
    dc = [{"date": "2009-11-01", "type": "DEATH CERTIFICATION", "grantor": "BROOKS J D JR", "grantee": "BROOKS PEGGY"}]
    assert ca.survivor_holds_title(dc, "BROOKS PEGGY") is None


def test_inheriting_years_after_the_obituary_means_a_namesake():
    deeds = [{"date": "2023-01-17", "type": "WILL TO OTHER THAN SURVIVING", "grantor": "WARD GERTRUDE V",
              "grantee": "WARD CAROLYN"}]
    assert ca._inherited_after("Carolyn Ward", deeds, "2019-11-29")
    assert ca._inherited_after("Carolyn Ward", deeds, "2022-06-01") is None   # late recording grace


def test_grantees_before_the_owners_vesting_deed_are_off_title():
    deeds = [
        {"date": "1992-02-28", "type": "ASSUMPTION DEED", "grantor": "FAIRES ROY LYNN", "grantee": "JOHNSON BRENDA"},
        {"date": "1981-02-19", "type": "WARRANTY DEED", "grantor": "JAMAIL EMILE", "grantee": "DLUGOS KARL J & LUZ E DLUGOS"},
    ]
    assert ca.recent_grantees(deeds, "JOHNSON BRENDA") == ["JOHNSON BRENDA"]
    assert len(ca.recent_grantees(deeds)) == 2   # without the owner, unchanged


def test_land_trust_is_an_investor():
    assert ca.is_entity("4910 SARA DR LAND TRUST ETAL")
    assert not ca.is_entity("VEDDER FAMILY TRUST")


def test_truncated_same_name_will_is_not_a_cure():
    deeds = [{"date": "2020-12-17", "type": "WILL TO OTHER THAN SURVIVING", "grantor": "CALDWELL DEMPSEY &",
              "grantee": "CALDWELL DEMPSEY &"}]
    assert ca.survivor_holds_title(deeds, "CALDWELL DEMPSEY &") is None


def test_travis_account_number_beats_a_straddling_city():
    assert ca.county_for_city("Leander", "78641", "01976802070000") == "Travis"
    assert ca.county_for_city("Leander", "78641", "R123456") == "Williamson"
    assert ca.county_for_city("Leander", "78641", "") == "Williamson"


# ── wrong-person guards, 2026-10-31 ──

def test_clerk_legal_must_be_this_property():
    street = "1705 Willow St"
    prop = "LOT 15 BLK 12 RIVERBEND SEC 2 AT UNIVERSITYHILLS"
    assert ca.legal_relation("LT 15 BLK 12 RIVERBEND SEC 2", prop, "6905 Burnell Dr") == "same"
    assert ca.legal_relation("X LOC 6905 BURNELL DRIVE AUSTIN", "", "6905 Burnell Dr") == "same"
    assert ca.legal_relation("LT 11 BLK A HAMILTON POINT", prop, street) == "other"
    assert ca.legal_relation("LT 3 BLK 12 RIVERBEND SEC 2", prop, street) == "other"
    assert ca.legal_relation("ABS 345 ACS 6.3 LOC 17700 HINES", "", "6905 Burnell Dr") == "other"
    bell = "NOLAN RIDGE PHASE 2, BLOCK 007, LOT 0020"
    assert ca.legal_relation("L20 B7 NOLAN RIDGE P2 N", bell, "216 Timber Ridge Dr") == "same"
    assert ca.legal_relation("L27 B1 SETTLERS PASS K", bell, "216 Timber Ridge Dr") == "other"
    for no_legal in ("SEE INSTRUMENT", "NO LEGAL", "$5139.06 + COSTS", "CAUSE NO. 3DC-25", ""):
        assert ca.legal_relation(no_legal, prop, street) == "unknown"


def test_vesting_deed_puts_a_prior_owners_death_off_title():
    deeds = [{"date": "2022-09-12", "type": "WARRANTY DEED", "grantor": "NOBLES ALMA L", "grantee": "MACKEY ANTON"},
             {"date": "2022-03-07", "type": "AFFIDAVIT OF HEIRSHIP", "grantor": "NOBLES ALMA L", "grantee": "MACKEY ANTON"}]
    assert ca.vesting_date(deeds, "MACKEY ANTON") == "2022-09-12"
    assert ca.vesting_date(deeds[1:], "MACKEY ANTON") == ""   # an heirship affidavit alone conveys nothing


def test_estate_of_fragments_are_not_heirs():
    heirs = [{"name": "Estate of Peggy Brooks", "relationship": "estate"}, {"name": "Of Garza", "relationship": "?"},
             {"name": "Teresa Schlickeisen", "relationship": "child"}, {"name": "Peggy A Brooks", "relationship": "?"}]
    assert [h["name"] for h in ca.real_heirs(heirs, "Peggy A Brooks")] == ["Teresa Schlickeisen"]


def test_court_applicant_check_accepts_a_first_name_only_survivor():
    case = {"pr_name": "Dorothy Lorrane Medlock"}
    assert not ca._obit_omits_applicant(case, [{"name": "Dorothy"}, {"name": "Jeff Medlock"}])
    assert ca._obit_omits_applicant({"pr_name": "Ying Z Lee"}, [{"name": "Rosemary Lee"}, {"name": "Jeanne McCracken"}])
    assert not ca._obit_omits_applicant({"pr_name": "Ying Z Lee"}, [])


def test_memorial_full_middle_name_is_not_just_an_initial():
    assert ca._full_middle_agrees("Virginia Ann Woolfolk", "Virginia Ann Woolfolk")
    assert not ca._full_middle_agrees("Jorge Luis Gonzales", "Jorge L Gonzales")


def test_a_real_estate_company_is_not_an_estate():
    assert not ca.ESTATE_RE.search("REAL ESTATE PROPERTY TEAM LLC")
    assert ca.ESTATE_RE.search("NASTAV DAVID HENRY ESTATE")
    assert ca.is_entity("REAL ESTATE PROPERTY TEAM LLC")


def test_a_living_co_owner_beside_an_estate_is_not_the_estate():
    deeds = [{"date": "2011-02-15", "grantor": "ROBLES, ABEL & DELFINA", "grantee": "ROBLES ABEL & ESTATE OF DELFINA ROBLES"}]
    assert ca._recorded_death("Abel Robles", deeds) == ""
    assert ca._recorded_death("Delfina Robles", deeds) == "2011-02-15"

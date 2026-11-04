import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import curative_roll as cr  # noqa: E402


def test_addr_key_ignores_directional_and_suffix_order():
    # DataSift rewrites '303 PHEASANT DR E' as '303 E Pheasant Dr'
    assert cr.addr_key("303 PHEASANT DR E", "78744") == cr.addr_key("303 E Pheasant Dr", "78744")
    assert cr.addr_key("1012 MILTON ST W", "78704") == ("1012", "MILTON", "78704")


def test_addr_key_drops_units_and_needs_a_house_number():
    assert cr.addr_key("8518 Cahill Dr Unit 7", "78729") == cr.addr_key("8518 CAHILL DR", "78729")
    assert cr.addr_key("Cr 430", "76574") is None
    assert cr.addr_key("Summer Lake Dr", "78654") is None


def _account(*streets):
    acct = cr.Account.__new__(cr.Account)
    acct.streets, acct.by_house, acct.bare = {}, {}, {}
    acct.apns = {cr._apn_key("Travis", "01000307170000"), cr._apn_key("Bell", "196775"),
                 cr._apn_key("Williamson", "r017129")}
    for st, z in streets:
        k = cr.addr_key(st, z)
        acct.streets.setdefault(k[:2], set()).add(z)
        acct.by_house.setdefault((k[0], z), []).append(set(k[1].split()))
    return acct


def test_account_match_by_parcel_and_address():
    acct = _account(("303 E Pheasant Dr", "78744"))
    assert acct.has("Travis", "01000307170000", "1 Nowhere Rd", "78704") == "parcel"
    assert acct.has("Bell", "0000196775", "", "") == "parcel"            # GSA pads to 10 digits
    assert acct.has("Williamson", "R017129", "", "") == "parcel"
    assert acct.has("Travis", "999", "303 PHEASANT DR E", "78744") == "address"
    assert acct.has("Travis", "999", "303 PHEASANT DR E", "78745-1234") == "address"   # ZIP rewritten
    assert acct.has("Travis", "999", "305 PHEASANT DR E", "78744") == ""


def test_travis_roll_street_formats_match_datasift():
    # the Travis roll drops ordinals: '2517 5 ST E' is DataSift's '2517 E 5th St'
    assert cr.addr_key("2517 5 St E", "78702") == cr.addr_key("2517 E 5Th St", "78702")
    assert cr.addr_key("2804 3 St E", "78702") == cr.addr_key("2804 E 3Rd St", "78702")
    assert cr.addr_key("10412 F M Rd 812", "78617") == cr.addr_key("10412 Fm 812", "78617")
    assert cr.addr_key("4304 U S Hy 183 S", "78744") == cr.addr_key("4304 S Us Highway 183", "78744")
    assert cr.addr_key("8301 State Hy 71 W", "78735") == cr.addr_key("8301 W Sh 71", "78735")
    assert cr.addr_key("401 County Road 370", "76537") == cr.addr_key("401 Cr 370", "76537")


def test_same_street_in_another_county_is_not_the_account_record():
    acct = _account(("1205 S 5Th St", "76504"))    # Temple
    assert acct.has("Travis", "999", "1205 5 St W", "78703") == ""   # Austin


def test_misspelled_street_same_house_and_zip_still_matches():
    acct = _account(("6405 Santos Street Rd", "78741"))
    assert acct.has("Travis", "999", "6405 Santos Ln", "78741") == "address"


def test_land_with_no_house_number_needs_the_owner_to_match():
    acct = _account()
    acct.bare[("HICKMAN", "78723")] = {"BERKMAN", "MANAGEMENT"}
    assert acct.has("Travis", "999", "Hickman Ave", "78723", "LEDGESTONE DEVELOPMENT GROUP LLC") == ""
    assert acct.has("Travis", "999", "Hickman Ave", "78723", "BERKMAN MANAGEMENT LLC") == "address + owner"


def test_owner_fields_per_county_format():
    # Travis / Williamson NAMELF, Bell 'LAST, FIRST', suffix not taken as the surname
    assert cr._owner_fields("MCLAUGHLIN MICHAEL JR") == {"owner_first": "Michael", "owner_last": "Mclaughlin",
                                                           "owner_company": ""}
    assert cr._owner_fields("MURPHY, BARBARA")["owner_last"] == "Murphy"
    assert cr._owner_fields("ESTATE OF RANDALL GAULT")["owner_last"] == "Gault"
    assert cr._owner_fields("SMITH FAMILY TRUST")["owner_company"] == "Smith Family Trust"

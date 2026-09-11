"""Shared TX trustee's-notice text parser (Bell + Williamson).

Run: PYTHONPATH=src python tests/test_trustee_notice_parse.py   (or pytest)
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers.trustee_notice_parse import (  # noqa: E402
    first_tuesday,
    parse_trustee_notice_text,
    sale_date_from_month_label,
    split_notices,
)

NOTICE_A = """NOTICE OF SUBSTITUTE TRUSTEE'S SALE
Deed of Trust Date: March 3, 2019. Grantor(s): JOHN Q PUBLIC AND JANE PUBLIC
WHEREAS, by a Deed of Trust executed by John Q Public, conveying to the Trustee
the real property commonly known as 123 Elm Street, Killeen, Texas 76541, and
WHEREAS default has occurred, NOW THEREFORE notice is hereby given that on
October 6, 2026, being the first Tuesday of said month, the Substitute Trustee
will sell the property at public auction at the Bell County courthouse between
10:00 AM and 1:00 PM. Substitute Trustee: Angela Zavala.
"""

NOTICE_B = """NOTICE OF TRUSTEE'S SALE
WHEREAS, by a Deed of Trust executed by Angela Zavala, conveying to a trustee,
the property commonly known as 9 Oak Cove, Round Rock, Texas 78664 was
encumbered. Sale will be held on the first Tuesday of the month at the
Williamson County courthouse, being the date shown on the posting. Further
details available from the substitute trustee listed herein.
"""


def test_fields_and_county():
    n = parse_trustee_notice_text(NOTICE_A, county="Bell", source_url="http://x/1.pdf")
    assert n is not None
    assert n.county == "Bell" and n.notice_type == "foreclosure" and n.state == "TX"
    assert n.address == "123 Elm Street", n.address
    assert n.city == "Killeen" and n.zip == "76541"
    assert n.owner_name == "John Q Public" and n.tax_owner_name == "John Q Public"
    assert n.auction_date == "2026-10-06", n.auction_date
    assert n.source_url == "http://x/1.pdf"
    assert n.raw_text.startswith("NOTICE OF SUBSTITUTE")


def test_substitute_trustee_blanked_and_fallback_sale_date():
    n = parse_trustee_notice_text(
        NOTICE_B, county="Williamson", fallback_sale_date="2026-11-03", date_added="2026-09-04",
    )
    assert n is not None
    assert n.owner_name == "" and n.tax_owner_name == ""
    assert n.address == "9 Oak Cove" and n.city == "Round Rock" and n.zip == "78664"
    assert n.auction_date == "2026-11-03"
    assert n.date_added == "2026-09-04"


def test_short_text_is_none():
    assert parse_trustee_notice_text("NOTICE OF TRUSTEE'S SALE short", county="Bell") is None


def test_nothing_usable_is_none():
    txt = "Lorem ipsum dolor sit amet " * 10
    assert parse_trustee_notice_text(txt, county="Bell") is None


def test_split_notices():
    chunks = split_notices(NOTICE_A + "\n" + NOTICE_B)
    assert len(chunks) == 2
    assert chunks[0].startswith("NOTICE OF SUBSTITUTE") and chunks[1].startswith("NOTICE OF TRUSTEE")
    assert split_notices("") == []


def test_first_tuesday_and_month_label():
    assert first_tuesday(2026, 10).strftime("%Y-%m-%d") == "2026-10-06"
    now = datetime(2026, 9, 9)
    assert sale_date_from_month_label("September Foreclosure Notices (4)", "", now) == "2026-09-01"
    assert sale_date_from_month_label("", "/files/Aug%205.pdf", now) == "2027-08-03"
    assert sale_date_from_month_label("no month here", "x.pdf", now) is None


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
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

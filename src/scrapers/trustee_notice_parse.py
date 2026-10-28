"""Texas trustee's-sale notice text → NoticeData. Shared by Bell + Williamson.

Moved out of foreclosure_bell (2026-09-09) so the Williamson scraper can parse
the clerk's individual notice PDFs (which have no index some months). The
patterns are county-agnostic — "commonly known as <addr>, <city>, Texas
<zip>", "executed by <borrower>", "<Month D, YYYY> ... Tuesday" — only the
county label differs.

Rules preserved from Bell:
  - a known TX substitute trustee caught by "executed by" leaves owner_name
    blank so the downstream CAD lookup fills the real borrower;
  - the pristine name is kept in tax_owner_name (papertrail);
  - a notice is returned only when it yielded an owner OR an address.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

MIN_NOTICE_CHARS = 100

ADDR_RE = re.compile(
    r"commonly\s+known\s+as\s*:?\s*"
    r"(\d{1,5}\s+[\w\s.'-]+?"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Circle|Cir|Court|Ct|Place|Pl|"
    r"Pike|Highway|Hwy|Trail|Trl|Terrace|Ter|Parkway|Pkwy|"
    r"Cove|Cv|Loop|Run|Path|Ridge|Crossing|Bend|Point|Pass)\.?)"
    r"\s*[,.]?\s*([\w\s]+?)"
    r"\s*[,.]?\s*(?:Texas|Tex\.?|TX)"
    r"\s*[,.\s]*(\d{5})?",
    re.IGNORECASE,
)

OWNER_RE = re.compile(
    r"executed\s+by\s+(.+?)(?:\s*,\s*(?:a|conveying|to\s|grantor|whose))",
    re.IGNORECASE,
)

SALE_DATE_RE = re.compile(
    r"(\w+\s+\d{1,2},?\s+\d{4})\s*(?:,\s*)?(?:being\s+)?(?:the\s+)?(?:first\s+)?(?:Tuesday|Tues)",
    re.IGNORECASE,
)

NOTICE_SPLIT_RE = re.compile(
    r"(?=NOTICE\s+OF\s+(?:SUBSTITUTE\s+)?TRUSTEE)", re.IGNORECASE,
)

# Month token (full or 3-letter abbreviation) for deriving the sale month from a
# PDF filename/label — Bell PDFs are named for the sale month ("Aug 5.pdf").
MONTH_TOKEN_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)
MON3_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def first_tuesday(year: int, month: int) -> datetime:
    """First Tuesday of the month — the statutory TX foreclosure sale day."""
    d = datetime(year, month, 1)
    return d + timedelta(days=(1 - d.weekday()) % 7)


def sale_date_from_month_label(label: str, href: str, now: datetime) -> str | None:
    """Sale date (YYYY-MM-DD) from a month name in the filename or link label.

    Prefers the href (deterministic filename month) over the label. A month
    earlier than the current one means next year's sale.
    """
    for source in (href, label):
        m = MONTH_TOKEN_RE.search(source or "")
        if not m:
            continue
        num = MON3_TO_NUM[m.group(1)[:3].lower()]
        year = now.year + (1 if num < now.month else 0)
        return first_tuesday(year, num).strftime("%Y-%m-%d")
    return None


def split_notices(text: str) -> list[str]:
    """Split a multi-notice PDF text on the NOTICE OF (SUBSTITUTE) TRUSTEE header."""
    return [
        chunk for chunk in NOTICE_SPLIT_RE.split(text or "")
        if chunk and len(chunk) >= MIN_NOTICE_CHARS
    ]


def _second_pass(notice: NoticeData, text: str) -> None:
    """Fill still-blank fields from notice_parser's TX-tuned generic patterns."""
    try:
        import notice_parser as np_
    except Exception:  # pragma: no cover
        return
    saved_raw = notice.raw_text
    notice.raw_text = text
    try:
        if not notice.address:
            np_._parse_address(notice)
        if not notice.auction_date:
            np_._parse_auction_date(notice)
    except Exception as e:  # never let the fallback break a parse
        logger.debug("trustee notice second pass failed: %s", e)
    finally:
        notice.raw_text = saved_raw


def parse_trustee_notice_text(
    text: str,
    *,
    county: str,
    source_url: str = "",
    fallback_sale_date: str | None = None,
    date_added: str | None = None,
) -> NoticeData | None:
    """Parse ONE trustee's-sale notice. None when nothing usable was found."""
    if not text or len(text) < MIN_NOTICE_CHARS:
        return None

    notice = NoticeData(
        notice_type="foreclosure",
        county=county,
        state="TX",
        date_added=date_added or datetime.now().strftime("%Y-%m-%d"),
        source_url=source_url,
    )

    addr_match = ADDR_RE.search(text)
    if addr_match:
        notice.address = addr_match.group(1).strip().title()
        notice.city = addr_match.group(2).strip().title()
        if addr_match.group(3):
            notice.zip = addr_match.group(3)

    owner_match = OWNER_RE.search(text)
    if owner_match:
        name = owner_match.group(1).strip()
        if name.isupper():
            name = name.title()
        from scrapers.trustee_blocklist import is_substitute_trustee
        if is_substitute_trustee(name):
            logger.info(
                "Skipped substitute trustee %r on %s foreclosure; "
                "leaving owner_name blank for CAD lookup", name, county,
            )
        else:
            notice.tax_owner_name = name  # PAPERTRAIL: preserve raw
            notice.owner_name = name

    sale_match = SALE_DATE_RE.search(text)
    if sale_match:
        raw = sale_match.group(1).strip()
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                notice.auction_date = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    notice.raw_text = text[:1000]
    _second_pass(notice, text)
    if notice.auction_date:
        notice.auction_date_source = "notice_text"

    if not notice.auction_date and fallback_sale_date:
        notice.auction_date = fallback_sale_date
        notice.auction_date_source = "folder_month"

    if notice.owner_name or notice.address:
        return notice
    return None

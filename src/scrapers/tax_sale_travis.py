"""Travis County tax sale scraper — RealAuction (travis.texas.realforeclose.com).

Travis County tax foreclosure sales run on RealAuction's RealForeclose
platform (first Tuesday monthly, 10:00 AM CT; property lists appear ~15 days
before the sale). The Tax Office page that `config.TRAVIS_TAX_SALES_URL` used
to point at is a 404 — the county now just links to RealAuction.

Everything needed is plain HTTP (no browser, no login) — discovered live
2026-08-18:

  1. CALENDAR: GET /index.cfm?zaction=USER&zmethod=CALENDAR
     [&selCalDate=MM/DD/YYYY for other months]. Days with a scheduled tax sale
     carry dayid='MM/DD/YYYY' on a CALBOX div whose text contains "TAX SALE".
  2. PREVIEW (primes the server-side session to that auction date):
     GET /index.cfm?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE=MM/DD/YYYY
  3. ITEMS: GET /index.cfm?zaction=AUCTION&Zmethod=UPDATE&FNC=LOAD&AREA=W
     &PageDir=0&doR=1&tx=<ms>&bypassPage=0 → JSON {"retHTML": ..., "rlist":
     "14527,14526,..."} where retHTML is templated HTML (@C/@B/@G shorthand)
     holding per-item: aid, Cause Number, Adjudged Value, Est. Min. Bid,
     Account Number (TCAD parcel), Property Address (truncated ~15 chars).
     AREA=W is "Auctions Waiting" (upcoming); AREA=C is closed/canceled.

The preview's Property Address is TRUNCATED, so the authoritative situs +
owner come from enrichment's parcel-first resolution (Account Number =
`parcel_id` keys straight into the Travis tax cache — same pattern as
fire_damage). The item-details view needs a bidder login; never fetch it.
"""

import logging
import re
import time
from datetime import datetime, timedelta

import requests

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

BASE = "https://travis.texas.realforeclose.com"
CALENDAR_URL = f"{BASE}/index.cfm?zaction=USER&zmethod=CALENDAR"
PREVIEW_URL = f"{BASE}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={{date}}"
LOAD_URL = (
    f"{BASE}/index.cfm?zaction=AUCTION&Zmethod=UPDATE&FNC=LOAD&AREA=W"
    f"&PageDir=0&doR=1&tx={{ts}}&bypassPage=0"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 2.0

# TAX SALE day cells in the calendar: dayid + "TAX SALE" in the same CALBOX div.
_SALE_DAY_RE = re.compile(
    r"dayid='(\d{2}/\d{2}/\d{4})'[^>]*>.{0,400}?TAX SALE", re.S
)
# One auction item in the templated retHTML: aid=".." ... up to the next item.
_ITEM_RE = re.compile(r'aid="(\d+)"(.*?)(?=aid="\d+"|\Z)', re.S)
# Label/value pairs inside an item's details table.
_FIELD_RE = re.compile(r'scope="row"[^>]*>\s*([^<@]+?):\s*</th><td[^>]*>\s*([^<@]*?)\s*@G')


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(s: requests.Session, url: str, referer: str | None = None) -> str:
    headers = {"Referer": referer} if referer else {}
    resp = s.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _find_sale_dates(s: requests.Session, months_ahead: int = 2) -> list[datetime]:
    """Read the RealAuction calendar for scheduled TAX SALE days."""
    dates: list[datetime] = []
    month_anchor = datetime.now().replace(day=1)
    for i in range(months_ahead + 1):
        # advance to the i-th month from now
        m = month_anchor
        for _ in range(i):
            m = (m + timedelta(days=32)).replace(day=1)
        url = CALENDAR_URL
        if i:
            url += f"&selCalDate={m.strftime('%m/%d/%Y')}"
        html = _get(s, url)
        for raw in _SALE_DAY_RE.findall(html):
            try:
                d = datetime.strptime(raw, "%m/%d/%Y")
            except ValueError:
                continue
            if d not in dates:
                dates.append(d)
        time.sleep(1.0)
    return sorted(dates)


def _parse_money(raw: str) -> str:
    cleaned = re.sub(r"[^\d.]", "", raw or "")
    return cleaned if cleaned else ""


def _parse_items(ret_html: str, sale_date: datetime) -> list[NoticeData]:
    notices: list[NoticeData] = []
    for m in _ITEM_RE.finditer(ret_html):
        aid, body = m.group(1), m.group(2)
        fields = {
            label.strip().lower(): value.strip()
            for label, value in _FIELD_RE.findall(body)
        }
        account = fields.get("account number", "")
        address = fields.get("property address", "")
        if not account and not address:
            continue

        notice = NoticeData(
            notice_type="tax_sale",
            county="Travis",
            state="TX",
            date_added=datetime.now().strftime("%Y-%m-%d"),
            auction_date=sale_date.strftime("%Y-%m-%d"),
            # Preview truncates the address (~15 chars). Enrichment's
            # parcel-first path (Account Number → Travis tax cache) supplies
            # the authoritative situs + owner, mirroring fire_damage.
            address=address.title(),
            city="",
            zip="",
            owner_name="",  # filled from CAD by parcel (ownerless-source rule)
            parcel_id=account,
            source_url=f"{BASE}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW"
                       f"&AUCTIONDATE={sale_date.strftime('%m/%d/%Y')}#AITEM_{aid}",
        )
        adjudged = _parse_money(fields.get("adjudged value", ""))
        min_bid = _parse_money(fields.get("est. min. bid", ""))
        if adjudged:
            notice.assessed_value = adjudged
            notice.assessed_source = "realauction_adjudged"
        if min_bid:
            # Min bid ≈ taxes + costs owed — the closest field we carry.
            notice.tax_delinquent_amount = min_bid
        notice.raw_text = (
            f"Travis tax sale {sale_date.strftime('%m/%d/%Y')} | "
            f"Cause: {fields.get('cause number', '')} | Acct: {account} | "
            f"Address: {address} | Adjudged: ${adjudged or '?'} | "
            f"Min bid: ${min_bid or '?'}"
        )
        notices.append(notice)
    return notices


def _backfill_situs(notices: list[NoticeData]) -> None:
    """Fill the truncated preview address + missing city/zip from the Travis
    tax cache by parcel (Account Number).

    The RealAuction preview truncates 'Property Address' to ~15 chars and
    carries no city/zip — without this, every record died at the pipeline's
    address validation ('missing city; missing zip', verified live on the
    first cloud run). Enrichment Step 5's ownerless path then resolves the
    owner from the same roll once the address exists.
    """
    try:
        import travis_tax_cache
    except ImportError:
        logger.warning("Travis tax sale: travis_tax_cache unavailable — records "
                       "will carry truncated addresses and no city/zip")
        return
    filled = 0
    for n in notices:
        if not n.parcel_id:
            continue
        try:
            rec = travis_tax_cache.search_by_parcel(n.parcel_id)
        except Exception as e:
            logger.warning("Travis tax sale: parcel situs lookup failed: %s", e)
            return
        if not rec:
            continue
        situs = (rec.get("situsaddress") or "").strip()
        if situs:
            n.address = situs.title()
        if not n.city:
            n.city = (rec.get("scity") or "").strip().title()
        if not n.zip:
            n.zip = (rec.get("szip") or "").strip()
        filled += 1
    logger.info("Travis tax sale: situs backfilled from tax roll for %d/%d "
                "parcels", filled, len(notices))


@register("Travis", "tax_sale")
class TravisTaxSaleScraper:
    """Upcoming Travis County tax sales from RealAuction (plain HTTP)."""

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        self.last_meta = {"returned": 0, "kept": 0}
        s = _session()

        try:
            sale_dates = _find_sale_dates(s)
        except requests.RequestException as e:
            from scrapers import ScraperError
            raise ScraperError(f"Travis tax sale: calendar fetch failed: {e}") from e

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        upcoming = [d for d in sale_dates if d >= today]
        logger.info(
            "Travis tax sale: calendar shows %d sale day(s), %d upcoming: %s",
            len(sale_dates), len(upcoming),
            [d.strftime("%m/%d/%Y") for d in upcoming],
        )
        if not upcoming:
            # Legitimately quiet between list postings (lists appear ~15 days
            # pre-sale) — but a scheduled-sale calendar with zero days EVER is
            # suspicious, so distinguish the two in the log.
            if not sale_dates:
                logger.warning(
                    "Travis tax sale: calendar shows NO tax-sale days at all in "
                    "%d months — calendar markup may have changed", 3,
                )
            return []

        notices: list[NoticeData] = []
        for sale_date in upcoming:
            date_str = sale_date.strftime("%m/%d/%Y")
            try:
                preview_url = PREVIEW_URL.format(date=date_str)
                _get(s, preview_url)  # primes the server-side session date
                raw = _get(
                    s,
                    LOAD_URL.format(ts=int(time.time() * 1000)),
                    referer=preview_url,
                )
            except requests.RequestException as e:
                logger.warning(
                    "Travis tax sale: preview/items fetch failed for %s: %s",
                    date_str, e,
                )
                continue

            # The endpoint prepends whitespace/HTML before the JSON body.
            start = raw.find('{"retHTML"')
            if start < 0:
                logger.warning(
                    "Travis tax sale: no JSON payload in items response for %s "
                    "(len=%d) — endpoint may have changed", date_str, len(raw),
                )
                from scrapers.debug_capture import dump_text
                dump_text(f"travis_tax_sale_no_json_{date_str.replace('/', '-')}", raw)
                continue
            import json
            try:
                payload = json.loads(raw[start:])
            except json.JSONDecodeError as e:
                logger.warning("Travis tax sale: bad JSON for %s: %s", date_str, e)
                continue

            items = _parse_items(payload.get("retHTML", ""), sale_date)
            listed = len((payload.get("rlist") or "").split(",")) if payload.get("rlist") else 0
            self.last_meta["returned"] += max(listed, len(items))
            logger.info(
                "Travis tax sale %s: %d item(s) parsed (%d listed)",
                date_str, len(items), listed,
            )
            if listed and not items:
                from scrapers import ScraperError
                from scrapers.debug_capture import dump_text
                dump_text(f"travis_tax_sale_parse_zero_{date_str.replace('/', '-')}",
                          payload.get("retHTML", ""))
                raise ScraperError(
                    f"Travis tax sale: {listed} auctions listed for {date_str} "
                    f"but 0 parsed — retHTML template changed",
                    partial=notices,
                )
            notices.extend(items)
            if max_notices and len(notices) >= max_notices:
                notices = notices[:max_notices]
                break
            time.sleep(REQUEST_DELAY)

        _backfill_situs(notices)

        self.last_meta["kept"] = len(notices)
        logger.info("Travis tax sale scrape complete: %d properties", len(notices))
        return notices

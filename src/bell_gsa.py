"""Bell CAD live parcel reader — the GSA portal (belltx-search.gsacorp.io).

Bell CAD replaced `esearch.bellcad.org` (Harris Govern PropertyAccess) with a
GSA Corp portal in 2026; the old host answers 404 for every path. The new one
is server-rendered, plain HTTP, no login, no captcha, no JS needed:

    /parcel/<10-digit prop_id>   owner + mailing, situs, use code, legal,
                                 DEED HISTORY (instrument, date, type,
                                 grantor, grantee), buildings, value history
    /tax/<10-digit prop_id>      per tax year: original, penalty, interest,
                                 paid, balance; account balance / currently due
    /api/livesearch/<query>      autocomplete by owner name, address or id
    /taxbill/RE/<id>/<year>      POST (CSRF token) -> tax statement PDF

What it does NOT have: receipts or a payer name (the Travis and Williamson
tax sites do — see docs/TAX_PAYER_HISTORY_FEASIBILITY.md). Prop IDs are
zero-padded to 10 digits in every URL; `/parcel/100046` redirects to
`/parcel/0000100046`, so pad before fetching and before comparing.

Why this module exists (2026-10-27): the batch delinquent XLSX on the CAD
data portal stopped being re-posted after 2026-07-17, so the roll-diff Sold
flow for Bell went blind. `fetch_tax` is the per-parcel truth for "is this
still delinquent / did it pay off", `fetch_parcel` for "who owns it now and
how did they get it" (an AFFIDAVIT OF HEIRSHIP in the deed table is a
curative-title lead on its own).

CLI:  python src/bell_gsa.py parcel 100046
      python src/bell_gsa.py tax 100046
      python src/bell_gsa.py search "GOMEZ, JOSE"
"""
from __future__ import annotations

import html as _html
import json
import logging
import random
import re
import sys
import time
from datetime import date

import requests

import config

logger = logging.getLogger(__name__)

BASE_URL = config.BELL_GSA_BASE_URL.rstrip("/")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)
# Project rule (CLAUDE.md): 2-3 s random delay between requests, 3 retries.
THROTTLE_RANGE_S = (2.0, 3.0)
RETRIES = 3
_last_call = 0.0


class BellParcelNotFound(LookupError):
    """The portal answered 200 but with no record (unknown / retired prop_id).

    `/parcel/<bogus>` returns an EMPTY 200 body and `/tax/<bogus>` a generic
    page that echoes the id with no owner and no tax years — so a plain parse
    would return blank fields and look like a parcel with nothing owed.
    """


# ── URLs ──────────────────────────────────────────────────────────────
def pad_id(prop_id: str | int | float) -> str:
    """GSA uses 10-digit zero-padded property ids everywhere.

    Accepts the shapes the roll hands us: int, `"100046"`, `"R-100046"`, and the
    float an openpyxl numeric cell can become (`100046.0` must not read as 1000460).
    """
    s = str(prop_id if prop_id is not None else "").strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".", 1)[0]
    s = re.sub(r"\D", "", s)
    return s.zfill(10) if s else ""


def parcel_url(prop_id: str | int) -> str:
    return f"{BASE_URL}/parcel/{pad_id(prop_id)}"


def tax_url(prop_id: str | int) -> str:
    return f"{BASE_URL}/tax/{pad_id(prop_id)}"


# ── HTTP ──────────────────────────────────────────────────────────────
def _get(url: str, session: requests.Session | None = None, timeout: int = 30) -> str:
    """Polite GET: random 2-3 s spacing, 3 retries with backoff on timeouts / 5xx.

    A 404 (retired parcel id) is raised immediately — retrying it is pointless.
    """
    global _last_call
    s = session or requests
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        wait = random.uniform(*THROTTLE_RANGE_S) - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = s.get(url, headers={"User-Agent": _UA}, timeout=timeout)
            _last_call = time.monotonic()
            if r.status_code == 404:
                r.raise_for_status()
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
            r.raise_for_status()
            return r.text
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise
            last_err = e
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        _last_call = time.monotonic()
        if attempt < RETRIES:
            backoff = 2.0 * attempt
            logger.warning("GSA GET %s failed (attempt %d/%d): %s — retry in %.0fs",
                           url, attempt, RETRIES, last_err, backoff)
            time.sleep(backoff)
    assert last_err is not None
    raise last_err


# ── HTML helpers ──────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    t = _TAG_RE.sub(" ", fragment or "")
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _money(s: str) -> float | None:
    """`$1,028.21` -> 1028.21; `-$12.40`, `$-12.40` and `($12.40)` -> -12.40 (credits)."""
    t = (s or "").strip()
    m = re.search(r"(\(?)\s*(-?)\s*\$?\s*(-?)\s*([\d,]+\.\d{2})\s*(\)?)", t)
    if not m:
        return None
    val = float(m.group(4).replace(",", ""))
    negative = bool(m.group(2) or m.group(3) or (m.group(1) and m.group(5)))
    return -val if negative else val


def _section(page: str, heading: str) -> str:
    """HTML from a <h2>/<h3> heading to the next heading (or end)."""
    m = re.search(r"<h[23][^>]*>\s*" + re.escape(heading) + r"\s*</h[23]>", page)
    if not m:
        return ""
    rest = page[m.end():]
    n = re.search(r"<h[23][^>]*>", rest)
    return rest[: n.start()] if n else rest


def _rows(table_html: str) -> list[list[str]]:
    """<tbody> rows as lists of cell text (td only)."""
    out = []
    body = table_html
    tb = re.search(r"<tbody>(.*?)</tbody>", table_html, re.S)
    if tb:
        body = tb.group(1)
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if cells:
            out.append([_text(c) for c in cells])
    return out


def _kv_table(fragment: str) -> dict[str, str]:
    """<tr><th>Key</th><td>Value</td></tr> tables -> dict."""
    out = {}
    for k, v in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", fragment, re.S):
        out[_text(k)] = _text(v)
    return out


def _owner_block(page: str) -> tuple[str, str, str]:
    """(owner name, mailing street, mailing city/state/zip).

    The parcel page wraps it in `<div class="ownership"><div>NAME<br><p>ADDR</p>`,
    the tax page in `<div class="owners-box"><div><p>NAME<br>ADDR...</p>`.
    """
    m = re.search(r'<div class="(?:ownership|owners-box)">(.*?)</div>\s*</div>', page, re.S)
    if not m:
        return "", "", ""
    inner = re.sub(r"</?p[^>]*>", "<br>", m.group(1))
    parts = [_text(x) for x in re.split(r"<br\s*/?>", inner)]
    parts = [x for x in parts if x]
    if not parts:
        return "", "", ""
    name = parts[0]
    street = parts[1] if len(parts) > 1 else ""
    csz = ", ".join(parts[2:]) if len(parts) > 2 else ""
    return name, street, csz


# ── Parcel page ───────────────────────────────────────────────────────
def parse_parcel(page: str) -> dict:
    """Parse /parcel/<id> HTML. Offline-testable (see tests/test_bell_gsa.py)."""
    out: dict = {
        "prop_id": "",
        "owner_name": "",
        "mailing_street": "",
        "mailing_city_state_zip": "",
        "situs": "",
        "use_code": "",
        "legal": "",
        "subdivision": "",
        "acreage": "",
        "deeds": [],
        "buildings": [],
        "value_history": {},
        "as_of": "",
    }
    m = re.search(r"Parcel\s+(\d{10})", page)
    if m:
        out["prop_id"] = m.group(1)

    out["owner_name"], out["mailing_street"], out["mailing_city_state_zip"] = _owner_block(page)

    summ = _kv_table(_section(page, "Parcel Summary"))
    out["situs"] = summ.get("Location", "")
    out["use_code"] = summ.get("Use Code", "")
    out["legal"] = summ.get("Legal Description", "")
    out["subdivision"] = summ.get("Subdivision", "")
    out["acreage"] = summ.get("Acreage", "")

    for r in _rows(_section(page, "Property Deed History")):
        if len(r) < 5:
            continue
        inst = re.sub(r"^\d+\s+", "", r[0]).strip()
        g = re.search(r"Grantor:\s*(.*?)\s*Grantee:\s*(.*)$", r[4])
        out["deeds"].append({
            "instrument": inst,
            "date": r[1],
            "type": r[2],
            "vacant_improved": r[3],
            "grantor": g.group(1).strip() if g else "",
            "grantee": g.group(2).strip() if g else r[4],
        })

    for r in _rows(_section(page, "Buildings")):
        if len(r) >= 4:
            out["buildings"].append({
                "type": r[0], "description": r[1], "year_built": r[2], "sqft": r[3],
            })

    vh = _section(page, "Value History")
    years = [int(y) for y in re.findall(r"<th[^>]*>\s*(20\d{2})\s*</th>", vh)]
    if years:
        hist: dict[int, dict] = {y: {} for y in years}
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", vh, re.S):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            if len(cells) == len(years) + 1:
                label = _text(cells[0]).rstrip("*")
                if label and not label.isdigit():
                    for y, c in zip(years, cells[1:]):
                        hist[y][label] = _text(c)
        out["value_history"] = hist

    # the apostrophe in "Assessor's" is HTML-escaped on the live page
    m = re.search(r"Office as of ([A-Za-z]+ \d{1,2}, \d{4})", page)
    if m:
        out["as_of"] = m.group(1)
    return out


def _assert_found(d: dict, kind: str, prop_id: str | int) -> None:
    if kind == "parcel":
        found = bool(d.get("prop_id")) and bool(d.get("owner_name") or d.get("situs"))
    else:
        found = bool(d.get("owner_name")) or bool(d.get("years"))
    if not found:
        raise BellParcelNotFound(f"Bell GSA {kind} page has no record for prop_id {pad_id(prop_id)}")


def fetch_parcel(prop_id: str | int, session: requests.Session | None = None) -> dict:
    page = _get(parcel_url(prop_id), session)
    d = parse_parcel(page)
    _assert_found(d, "parcel", prop_id)
    d["url"] = parcel_url(prop_id)
    return d


# ── Tax page ──────────────────────────────────────────────────────────
def parse_tax(page: str, today: date | None = None) -> dict:
    """Parse /tax/<id> HTML into per-year rows plus a delinquency summary.

    A tax year with a balance is delinquent once its 01/31 due date has passed
    (Tex. Tax Code §31.02): year Y is "current" (not yet delinquent) only while
    `today <= Jan 31, Y+1`. From Feb 1 the unpaid current-year bill counts. The
    page's own "<Y> Tax Year" block is still parsed, but its Original/Total
    split is what carries penalty and interest (the block shows no breakdown).
    """
    today = today or date.today()
    out: dict = {
        "prop_id": "",
        "owner_name": "",
        "mailing_street": "",
        "mailing_city_state_zip": "",
        "situs": "",
        "balance": None,
        "currently_due": None,
        "current_year": None,
        "years": [],
        "as_of": "",
    }
    m = re.search(r"Tax Account\s+(\d{10})", page)
    if m:
        out["prop_id"] = m.group(1)
    out["owner_name"], out["mailing_street"], out["mailing_city_state_zip"] = _owner_block(page)
    summ = _kv_table(_section(page, "Account Summary"))
    out["situs"] = summ.get("Location", "")
    out["balance"] = _money(summ.get("Balance", ""))
    out["currently_due"] = _money(summ.get("Currently Due", ""))
    m = re.search(r"<h3[^>]*>\s*(20\d{2}) Tax Year\s*</h3>", page)
    if m:
        out["current_year"] = int(m.group(1))
    m = re.search(r"balances are as of (\d{2}/\d{2}/\d{4})", page)
    if m:
        out["as_of"] = m.group(1)

    rows: list[dict] = []
    # Current-year block: Description / Original / Total / Paid / Balance as a kv table.
    cur = _section(page, f"{out['current_year']} Tax Year") if out["current_year"] else ""
    if cur:
        kv = _kv_table(cur)
        original = _money(kv.get("Original", ""))
        total = _money(kv.get("Total", ""))
        rows.append({
            "year": out["current_year"],
            "original": original,
            "penalty": None,  # the current-year block shows no P&I breakdown
            "interest": round(total - original, 2) if (total is not None and original is not None) else None,
            "paid": _money(kv.get("Paid", "")),
            "balance": _money(kv.get("Balance", "")),
            "current": True,
        })
    for r in _rows(_section(page, "Past Years")):
        ym = re.match(r"(20\d{2}|19\d{2})\b", r[0]) if r else None
        if not ym or len(r) < 7:
            continue
        rows.append({
            "year": int(ym.group(1)),
            "original": _money(r[1]),
            "penalty": _money(r[2]),
            "interest": _money(r[3]),
            "paid": _money(r[4]),
            "balance": _money(r[6]),
            "current": False,
        })
    for r in rows:
        r["due_date"] = date(r["year"] + 1, 1, 31).isoformat()
        r["delinquent"] = (r["balance"] or 0) > 0 and today > date(r["year"] + 1, 1, 31)
    out["years"] = rows
    delq = [r for r in rows if r["delinquent"]]
    out["delinquent_years"] = len(delq)
    out["delinquent_balance"] = round(sum(r["balance"] or 0 for r in delq), 2)
    out["oldest_delinquent_year"] = min((r["year"] for r in delq), default=None)
    paid_rows = [r for r in rows if (r["paid"] or 0) > 0]
    out["last_paid_year"] = max((r["year"] for r in paid_rows), default=None)
    return out


def fetch_tax(prop_id: str | int, session: requests.Session | None = None) -> dict:
    page = _get(tax_url(prop_id), session)
    d = parse_tax(page)
    _assert_found(d, "tax", prop_id)
    d["url"] = tax_url(prop_id)
    return d


# ── Live search ───────────────────────────────────────────────────────
def parse_livesearch(payload: str | dict) -> list[dict]:
    data = json.loads(payload) if isinstance(payload, str) else payload
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", data.get("html", ""), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cells) < 4:
            continue
        pid = re.search(r"/parcel/(\d{10})", cells[0])
        rows.append({
            "prop_id": pid.group(1) if pid else _text(cells[0]),
            "kind": _text(re.split(r"<span", cells[1], 1)[0]),
            "has_tax": "/tax/" in cells[1],
            "name": _text(cells[2]),
            "address": _text(cells[3]),
        })
    # the API repeats a row per record type; keep one per (id, name, address)
    seen, out = set(), []
    for r in rows:
        k = (r["prop_id"], r["name"], r["address"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def livesearch(query: str, session: requests.Session | None = None) -> list[dict]:
    from urllib.parse import quote
    return parse_livesearch(_get(f"{BASE_URL}/api/livesearch/{quote(query)}", session))


# ── CLI ───────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["parcel", "tax", "search"])
    ap.add_argument("value", help="prop_id (parcel/tax) or query text (search)")
    args = ap.parse_args(argv)
    if args.what == "parcel":
        print(json.dumps(fetch_parcel(args.value), indent=2))
    elif args.what == "tax":
        print(json.dumps(fetch_tax(args.value), indent=2))
    else:
        print(json.dumps(livesearch(args.value), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""County-clerk land records by OWNER NAME -> encumbrances (curative Gate 2).

Operator-run (headed browsers, the home residential IP). One session per
county, names searched sequentially, every result cached per name under
data/curative_audit/cache/clerk/. Read-only public searches; nothing here
costs money.

    Travis      tccsearch.org Party Name search, filtered to the encumbrance
                doc-type checkboxes (a name search over every doc type and 50
                years times out at the origin: Cloudflare 524)
    Bell        bell.tx.publicsearch.us quick search by name (URL, limit=250)
    Williamson  Tyler Self-Service "Both Names" (persistent profile so the
                AWS WAF clearance survives between runs)

The clerk index carries parties, doc type, date and instrument number - never
the dollar amount. Amounts on deeds of trust, abstracts of judgment and tax
liens are only on the document image, so every encumbrance found here is
reported with its amount UNKNOWN unless another source supplies one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("clerk_records")

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "curative_audit" / "cache" / "clerk"
TYLER_PROFILE = ROOT / ".tyler_profile"

# Order matters: a RELEASE OF LIEN is a release, not a lien.
DOC_CLASSES: list[tuple[str, re.Pattern]] = [(k, re.compile(p, re.I)) for k, p in (
    ("release", r"RELEASE|SATISFACTION|RECONVEY|WAIVER|\bLR\b|TERMINATION"),
    ("foreclosure", r"TRUSTEE.?S?\s+SALE|SUBSTITUTE TRUSTEE|APPT OF SUB|APPOINTMENT OF (SUB|TRUSTEE)|NOTICE OF SALE|"
                    r"NOTICE OF (DEFAULT|ACCELERATION|FORECLOSURE)|FORECLOS|PENDENS"),
    ("mortgage", r"DEED OF TRUST|\bD/?T\b|MORTGAGE|HOME EQUITY|REVERSE|SECURITY INSTRUMENT|MODIFICATION|MASTER DEED OF TR"),
    ("tax_lien", r"FEDERAL TAX|STATE TAX|ESTATE TAX|\bIRS\b|TAX LIEN"),
    ("judgment", r"ABSTRACT|JUDG"),
    ("mechanics", r"MECHANIC|MATERIALM|BROKERS? LIEN|CONTRACTOR"),
    ("child_support", r"CHILD SUPPORT|\bCS LIEN"),
    ("hospital", r"HOSPITAL"),
    ("medicaid", r"MEDICAID|HHSC|ESTATE RECOVERY"),
    ("hoa", r"ASSESSMENT LIEN|\bHOA\b|OWNERS? ASS|ASSOCIATION LIEN|MAINTENANCE LIEN"),
    ("bankruptcy", r"BANKRUPT"),
    ("lien", r"\bLIEN\b"),
)]
ENCUMBRANCES = {"mortgage", "foreclosure", "tax_lien", "judgment", "mechanics", "child_support",
                "hospital", "medicaid", "hoa", "bankruptcy", "lien"}

LABELS = {
    "mortgage": "deed of trust / mortgage", "foreclosure": "FORECLOSURE posted (trustee sale / lis pendens)",
    "tax_lien": "federal/state tax lien", "judgment": "judgment lien (abstract of judgment)",
    "mechanics": "mechanic's / contractor lien", "child_support": "child support lien",
    "hospital": "hospital lien (attaches to an injury claim, not the house - Prop. Code 55.005(d))",
    "medicaid": "Medicaid estate recovery claim", "hoa": "HOA / assessment lien", "bankruptcy": "bankruptcy",
    "lien": "lien",
}

# Travis tccsearch doc-type checkbox indices (captured live 2026-10-29).
TCC_DOC_TYPES = [1, 8, 12, 14, 19, 24, 27, 28, 37, 42, 49, 51, 52, 54, 56, 57, 60, 62, 63, 64, 65, 67, 72,
                 77, 78, 79, 82, 83, 93, 94, 97, 99, 100, 105, 106, 107, 108, 109]
MAX_ROWS = 250   # a person with more name hits than this is "too common for a name search"


def doc_class(doc_type: str) -> str:
    for k, rx in DOC_CLASSES:
        if rx.search(doc_type or ""):
            return k
    return "other"


def _iso(mdy: str) -> str:
    try:
        return datetime.strptime(mdy.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


# ── parsing (pure, unit-tested) ──────────────────────────────────────

_TCC_DATE_LINE = re.compile(r"^(\d{2}/\d{2}/\d{4})\t")
_TCC_INSTR = re.compile(r"(\d{8,})")
_TCC_R = re.compile(r"\[R\]\s*(.+?)(?:\s*\(\+\))?$")
_TCC_E = re.compile(r"\[E\]\s*(.+?)(?:\s*\(\+\))?$")


def parse_tcc_grid(body_text: str) -> list[dict]:
    """tccsearch results grid inner_text -> rows (same anchoring as lien_travis._parse_body_text)."""
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    out = []
    for i, line in enumerate(lines):
        m = _TCC_DATE_LINE.match(line)
        if not m:
            continue
        instr = ""
        for k in range(i - 1, max(-1, i - 5), -1):
            mm = _TCC_INSTR.search(lines[k])
            if mm:
                instr = mm.group(1)
                break
        if not instr:
            continue
        parts = line.split("\t")
        row = {"county": "Travis", "instrument": instr, "date": _iso(m.group(1)),
               "doc_type": parts[1].strip() if len(parts) > 1 else "", "grantors": [], "grantees": [], "legal": "",
               "url": f"https://www.tccsearch.org/RealEstate/SearchEntry.aspx#InstrumentNumber={instr}"}
        for seg in [line] + lines[i + 1:i + 5]:
            if _TCC_DATE_LINE.match(seg) and seg is not line:
                break
            r, e = _TCC_R.search(seg), _TCC_E.search(seg)
            if r:
                row["grantors"].append(r.group(1).strip())
            if e:
                row["grantees"].append(e.group(1).strip())
            if "LOC " in seg.upper() or re.search(r"\bLOT\b|\bBLK\b", seg, re.I):
                row["legal"] = seg
        out.append(row)
    return out


def parse_govos_rows(cells_rows: list[list[str]], county: str = "Bell") -> list[dict]:
    """GovOS results table rows (cells) -> rows. Columns: _, _, _, GRANTOR, GRANTEE, DOC TYPE,
    RECORDED DATE, INST NUMBER, BOOK/VOL/PAGE, PROPERTY DESCRIPTION."""
    out = []
    for c in cells_rows:
        c = [x.strip() for x in c]
        if len(c) < 8:
            continue
        g = c[3:] if len(c) >= 10 else c[-7:] if len(c) >= 7 else c
        grantor, grantee, typ, rec, instr = g[0], g[1], g[2], g[3], g[4]
        legal = g[6] if len(g) > 6 else ""
        if not re.match(r"\d{1,2}/\d{1,2}/\d{4}", rec):
            continue
        out.append({"county": county, "instrument": instr, "date": _iso(rec), "doc_type": typ,
                    "grantors": [x.strip() for x in grantor.split("\n") if x.strip()],
                    "grantees": [x.strip() for x in grantee.split("\n") if x.strip()],
                    "legal": legal.replace("Property Description:", "").strip(),
                    "url": f"https://{county.lower()}.tx.publicsearch.us/doc/{instr}"})
    return out


def parse_tyler_rows(rows: list[dict]) -> list[dict]:
    from scrapers.tyler_common import BASE, parse_h1  # noqa: PLC0415
    out = []
    for r in rows:
        instr, typ, d = parse_h1(r.get("h1") or "")
        out.append({"county": "Williamson", "instrument": instr, "date": d, "doc_type": typ,
                    "grantors": r.get("grantors") or [], "grantees": r.get("grantees") or [],
                    "legal": "; ".join(r.get("legal") or []),
                    # href is site-absolute ('/williamsonweb/document/...'): join it to the host
                    "url": f"{re.match(r'https://[^/]+', BASE).group(0)}{r.get('href')}" if r.get("href") else BASE})
    return out


# ── matching + summarising (pure) ────────────────────────────────────

def _toks(s: str) -> list[str]:
    return [t for t in re.findall(r"[A-Z']+", (s or "").upper()) if t not in ("ETUX", "ETAL", "ET", "AL", "UX")]


def party_is(person_first_last: str, party: str) -> bool:
    """Index parties are 'LAST FIRST MIDDLE' (sometimes 'LAST, FIRST'). First AND last must both
    appear, and a middle initial on both sides must agree."""
    p = _toks(person_first_last)
    q = _toks(party)
    if len(p) < 2 or len(q) < 2:
        return False
    first, last = p[0], p[-1]
    if p[-1] in ("JR", "SR", "II", "III", "IV") and len(p) > 2:
        last = p[-2]
    if first not in q or last not in q:
        return False
    pm = [t for t in p[1:-1] if t not in ("JR", "SR", "II", "III", "IV")]
    qm = [t for t in q if t not in (first, last, "JR", "SR", "II", "III", "IV")]
    if pm and qm and pm[0][0] != qm[0][0] and len(qm) == 1:
        return False
    return True


def _creditor(row: dict, person: str) -> str:
    others = [x for x in row["grantors"] + row["grantees"] if not party_is(person, x)]
    return others[0] if others else ""


def encumbrances(rows: list[dict], person: str, since: str = "") -> list[dict]:
    """Encumbrances naming this person, oldest first, each marked released when a later
    release names the same creditor. `since` = the date they took title (judgments and
    tax liens are kept from 10 years earlier: an abstract attaches to after-acquired land)."""
    mine = [r for r in rows if any(party_is(person, x) for x in r["grantors"] + r["grantees"])]
    for r in mine:
        r["class"] = doc_class(r["doc_type"])
    releases = [r for r in mine if r["class"] == "release"]
    out = []
    for r in sorted(mine, key=lambda x: x["date"] or ""):
        if r["class"] not in ENCUMBRANCES:
            continue
        floor = since
        if since and r["class"] in ("judgment", "tax_lien", "child_support"):
            floor = f"{int(since[:4]) - 10}{since[4:]}"
        if floor and (r["date"] or "") < floor:
            continue
        cred = _creditor(r, person)
        ct = {t for t in _toks(cred) if len(t) >= 4} - {"BANK", "MORTGAGE", "NATIONAL", "ASSOCIATION", "COMPANY",
                                                         "TRUST", "SERVICES", "FINANCIAL", "LENDING", "TEXAS"}
        rel = next((x for x in releases if (x["date"] or "") >= (r["date"] or "")
                    and (not ct or ct & {t for t in _toks(" ".join(x["grantors"] + x["grantees"]))})), None)
        out.append({**r, "creditor": cred, "released": bool(rel),
                    "released_by": f"{rel['doc_type']} {rel['date']} #{rel['instrument']}" if rel else ""})
    return out


# Years a recorded item stays live without a release (Texas):
#  abstract of judgment - 10 yrs from recording unless renewed (CPRC 34.001, Prop. Code 52.006)
#  state tax lien 10 yrs; federal tax lien 10 yrs (refiled when extended)
#  mechanic's lien - suit within 1-2 yrs (Prop. Code 53.158)
#  deed of trust - a loan older than 30 yrs is paid off in practice, but still needs a release for title
#  foreclosure postings - a sale notice older than 3 yrs is history
MAX_AGE = {"judgment": 10, "tax_lien": 10, "mechanics": 2, "mortgage": 30, "foreclosure": 3, "lien": 10,
           "hospital": 10, "bankruptcy": 10}


def _age(e: dict) -> float:
    try:
        return (date.today() - date.fromisoformat(e["date"])).days / 365.25
    except (TypeError, ValueError):
        return 0.0


def open_items(encs: list[dict]) -> list[dict]:
    """Unreleased items still within their live period (child support / HOA / Medicaid never age out)."""
    return [e for e in encs if not e["released"] and _age(e) <= MAX_AGE.get(e["class"], 99)]


def stale_items(encs: list[dict]) -> list[dict]:
    """Unreleased but past their live period - no money owed, but a title company may want a release."""
    return [e for e in encs if not e["released"] and _age(e) > MAX_AGE.get(e["class"], 99)]


# ── live searches ────────────────────────────────────────────────────

def signed_after(county: str, name: str, after: str) -> dict | None:
    """A cached clerk row where `name` is a GRANTOR recorded after `after` (ISO date): the person
    was alive to sign it. Every cached search window for that name is read."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    for f in CACHE.glob(f"{county}_{slug}_*.json"):
        try:
            rows = json.loads(f.read_text(encoding="utf-8")).get("rows") or []
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            if (r.get("date") or "") > after and any(party_is(name, g) for g in r.get("grantors") or []):
                return r
    return None


def _cache_key(county: str, name: str, since: str) -> Path:
    return CACHE / f"{county}_{re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_')}_{since[:4] or 'all'}.json"


def cached(county: str, name: str, since: str) -> dict | None:
    import json  # noqa: PLC0415
    p = _cache_key(county, name, since)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save(county: str, name: str, since: str, res: dict) -> None:
    import json  # noqa: PLC0415
    p = _cache_key(county, name, since)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, indent=1), encoding="utf-8")


def _namelf(first_last: str) -> str:
    t = _toks(first_last)
    if len(t) >= 2 and t[-1] in ("JR", "SR", "II", "III", "IV"):
        t = t[:-1]
    return f"{t[-1]} {t[0]}" if len(t) >= 2 else " ".join(t)


async def _search_bell(page, name: str, since: str) -> dict:
    from urllib.parse import quote  # noqa: PLC0415
    base = "https://bell.tx.publicsearch.us"
    rng = f"&recordedDateRange={since.replace('-', '')}%2C{date.today():%Y%m%d}" if since else ""
    url = (f"{base}/results?department=RP&searchType=quickSearch&searchValue={quote(_namelf(name))}"
           f"&limit={MAX_ROWS}{rng}")
    # A second results URL in the same tab renders "No results found" for ANY
    # name (even a bare surname) - 238 of 239 searches silently read zero on
    # 2026-10-30. A fresh tab per search returns the real results.
    tab = await page.context.new_page()
    try:
        await tab.goto(url, wait_until="domcontentloaded")
        # big result sets (common names) render slower than the old fixed 7 s
        try:
            await tab.wait_for_function(
                r"() => /of [\d,]+ results|No results found/.test(document.body.innerText)", timeout=25000)
        except Exception:  # noqa: BLE001
            pass
        await tab.wait_for_timeout(1500)
        body = await tab.evaluate("() => document.body.innerText")
        m = re.search(r"of ([\d,]+) results", body)
        if not m and "No results found" not in body:
            raise RuntimeError("GovOS results never rendered")
        total = int(m.group(1).replace(",", "")) if m else 0
        cells = await tab.evaluate("() => [...document.querySelectorAll('tbody tr')].map(r => [...r.cells].map(c => c.innerText))")
    finally:
        await tab.close()
    return {"total": total, "rows": parse_govos_rows(cells, "Bell"), "truncated": total > MAX_ROWS}


async def _search_travis(page, name: str, since: str) -> dict:
    from scrapers import lien_travis as L  # noqa: PLC0415
    from scrapers import tccsearch_common as C  # noqa: PLC0415
    await C.goto_with_retry(page, L.SEARCH_URL)
    await C.wait_ready(page)
    for idx in TCC_DOC_TYPES:
        await C.safe_check(page, f"#cphNoMargin_f_dclDocType_{idx}", timeout_ms=5000)
    await page.check("#cphNoMargin_f_rdoCombine")
    await page.fill("#cphNoMargin_f_txtParty", _namelf(name))
    await L._set_date_range(page, datetime.strptime(since or "1980-01-01", "%Y-%m-%d"), datetime.now())
    await page.click("#cphNoMargin_SearchButtons1_btnSearch", no_wait_after=True)
    await page.wait_for_url("**/SearchResults*", timeout=150000)
    await page.wait_for_timeout(2500)
    body = await page.inner_text("body")
    if "Error code 524" in body or "timed out" in body.lower():
        raise RuntimeError("tccsearch origin timeout (524)")
    rows = parse_tcc_grid(body)
    pg = 2
    while len(rows) < MAX_ROWS and await L._go_to_page(page, pg):
        rows += parse_tcc_grid(await page.inner_text("body"))
        pg += 1
    return {"total": len(rows), "rows": rows, "truncated": len(rows) >= MAX_ROWS}


async def _post_williamson(page, name: str, since: str, fresh: bool) -> int:
    """Fill the Document Search form for one name and submit. -> searchPost status."""
    from scrapers import tyler_common as T  # noqa: PLC0415
    if not fresh and await page.locator("#field_BothNamesID").is_visible():
        await page.get_by_text("Clear Selections").first.click()
        await page.wait_for_timeout(1000)
    elif not await T.accept_disclaimer_and_open_search(page):
        raise RuntimeError("Tyler search form never rendered (AWS WAF)")
    # The name box is a chip autocomplete: fill() shows the chip but the form
    # never posts. Typing + Tab (the portal's own hint) commits it.
    box = page.locator("#field_BothNamesID")
    await box.wait_for(state="visible", timeout=30000)
    await box.click()
    await box.press_sequentially(_namelf(name), delay=60)
    await page.wait_for_timeout(1200)
    await box.press("Tab")
    await T.set_window_dates(page, datetime.strptime(since or "1980-01-01", "%Y-%m-%d"), datetime.now())
    async with page.expect_response(lambda r: "searchPost" in r.url, timeout=60000) as posted:
        await T.click_search(page)
    return (await posted.value).status


async def _search_williamson(page, name: str, since: str) -> dict:
    from scrapers import tyler_common as T  # noqa: PLC0415
    # Clear the form and wait for the searchPost answer for every name: the old
    # flow fetched the PREVIOUS search's rows (two owners got the same 36 rows
    # on 2026-10-30) or nothing. Re-running the disclaimer flow per search trips
    # the AWS WAF by the third name, so it is only the recovery for a 405.
    status = await _post_williamson(page, name, since, fresh=False)
    if status == 405:
        await page.wait_for_timeout(20000)
        status = await _post_williamson(page, name, since, fresh=True)
    if status >= 400:
        raise RuntimeError(f"Tyler searchPost HTTP {status}")
    await page.wait_for_timeout(2500)
    rows = []
    for n in range(1, 6):
        res = await T.fetch_results_page(page, n)
        if n == 1 and res.get("status") == 405:
            await page.wait_for_timeout(20000)
            if await _post_williamson(page, name, since, fresh=True) < 400:
                await page.wait_for_timeout(2500)
                res = await T.fetch_results_page(page, n)
        if not res.get("ok"):
            if n == 1:
                raise RuntimeError(f"Tyler results HTTP {res.get('status')}")
            break
        if not res.get("rows"):
            break
        rows += parse_tyler_rows(res["rows"])
    surname = _toks(_namelf(name))[:1]
    if rows and surname and not any(surname[0] in _toks(" ".join(r["grantors"] + r["grantees"])) for r in rows):
        raise RuntimeError(f"Tyler returned rows without {surname[0]} - stale results")
    return {"total": len(rows), "rows": rows, "truncated": len(rows) >= MAX_ROWS}


async def _open(p, county: str):
    """(closer, page) for one county session."""
    if county == "Travis":
        from scrapers import lien_travis as L  # noqa: PLC0415
        from scrapers import tccsearch_common as C  # noqa: PLC0415
        browser, ctx, page = await C.launch_tcc_context(p)
        await L._accept_disclaimer(page)
        return browser.close, page
    if county == "Williamson":
        from scrapers import tyler_common as T  # noqa: PLC0415
        ctx = await p.chromium.launch_persistent_context(
            str(TYLER_PROFILE), headless=False, user_agent=T.USER_AGENT, viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"])
        await ctx.add_init_script(T.INIT_SCRIPT)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if not await T.accept_disclaimer_and_open_search(page):
            await ctx.close()
            raise RuntimeError("Tyler search form never rendered (AWS WAF) - retry in ~30 min")
        return ctx.close, page
    browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    page = await (await browser.new_context(viewport={"width": 1400, "height": 900})).new_page()
    await page.goto("https://bell.tx.publicsearch.us", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    return browser.close, page


# Williamson's AWS WAF re-arms after ~4 quick searches.
PACE_S = {"Williamson": 12}

SEARCHERS = {"Travis": _search_travis, "Bell": _search_bell, "Williamson": _search_williamson}


def search_people(jobs: list[tuple[str, str, str]]) -> dict:
    """jobs = [(county, 'First M Last', since_iso)] -> {(county,name,since): result}. Cached per job.

    Uses the home connection (SCRAPER_PROXY_URL is blanked: a residential
    proxy costs money and the operator box is already a US residential IP)."""
    from playwright.async_api import async_playwright  # noqa: PLC0415
    os.environ["SCRAPER_PROXY_URL"] = ""
    todo = [j for j in jobs if cached(*j) is None]
    out = {j: cached(*j) for j in jobs if cached(*j) is not None}

    async def run():
        async with async_playwright() as p:
            for county in ("Bell", "Travis", "Williamson"):
                mine = [j for j in todo if j[0] == county]
                if not mine:
                    continue
                try:
                    close, page = await _open(p, county)
                except Exception as e:  # noqa: BLE001
                    logger.error("%s clerk session failed: %s", county, e)
                    continue
                fails = 0
                try:
                    for j in mine:
                        try:
                            res = await SEARCHERS[county](page, j[1], j[2])
                        except Exception as e:  # noqa: BLE001
                            fails += 1
                            logger.warning("%s clerk %s: %s", county, j[1], str(e)[:160])
                            if fails >= 3:
                                logger.error("%s clerk: 3 failures - stopping this county for now", county)
                                break
                            continue
                        fails = 0   # 3 failures IN A ROW stop the county, not 3 over hundreds
                        res["searched"] = datetime.now().isoformat(timespec="seconds")
                        _save(*j, res)
                        out[j] = res
                        logger.info("%s clerk %-28s since %s -> %d rows%s", county, j[1], j[2] or "-",
                                    res["total"], " (TRUNCATED)" if res["truncated"] else "")
                        await asyncio.sleep(PACE_S.get(county, 2))
                finally:
                    await close()
    if todo:
        asyncio.run(run())
    return out

"""Name-indexed OPR deed/instrument search — {county}.tx.publicsearch.us (GovOS).

Deep-prospecting research tool. The lien scraper sweeps this same portal by
doc-type + date range; this one searches by PARTY NAME with no date limit, which
is what you need to answer "who actually owns this / who are the heirs" on an
estate record.

Typical use: a tax-delinquent parcel whose CAD owner is "SMITH, JOHN EST" with a
null deed date. Search the name as GRANTEE (the deed INTO them) and as GRANTOR
(anything conveyed out, plus estate filings). An Affidavit of Heirship in the
results names the heirs outright.

╔═══════════════════════════════════════════════════════════════════════════╗
║ MUST RUN HEADED — same anti-bot as the lien scraper. Headless sticks on    ║
║ "Loading Search Results..." forever and the search API never fires.        ║
║ On a headless server: xvfb-run -a python src/deed_search.py ...            ║
╚═══════════════════════════════════════════════════════════════════════════╝

Usage:
  python src/deed_search.py --county Bell --name "MEDRANO SEBASTIAN"
  python src/deed_search.py --county Bell --name "MEDRANO SEBASTIAN" --party grantee
  python src/deed_search.py --county Williamson --name "DOE JANE" --json out.json

Note the party semantics are the OPPOSITE of the lien rule: on a deed the
GRANTEE is the person who RECEIVED title (the owner you care about), and the
GRANTOR conveyed it away.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys

from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

SUBDOMAINS = {"bell": "bell", "williamson": "williamson"}

# Instrument types that bear on ownership / heirship. Used to HIGHLIGHT rows, not
# to filter the search — an unindexed or oddly-labelled instrument is exactly what
# you are hunting on an estate, so everything is returned.
KEY_TYPES = [
    "AFFIDAVIT OF HEIRSHIP", "HEIRSHIP", "WARRANTY DEED", "SPECIAL WARRANTY DEED",
    "DEED WITHOUT WARRANTY", "QUIT CLAIM", "QUITCLAIM", "DEED", "PROBATE",
    "MUNIMENT", "WILL", "DEATH CERTIFICATE", "EXECUTOR", "ADMINISTRATOR",
    "GIFT DEED", "CORRECTION DEED", "TRUSTEE",
]

# Same overlay-killer as the lien scraper: Beamer push modal / NPS iframe / the
# GovOS product tour all swallow pointer events on the advanced-search form.
_DISMISS_JS = """() => {
  ['#beamerPushModal', '#npsIframeContainer', '.beamer_overlay',
   '[class*="Beamer"]'].forEach(s => {
     document.querySelectorAll(s).forEach(e => e.remove());
  });
  const byText = [...document.querySelectorAll('button,a')].find(b => {
     const t = (b.innerText || '').trim().toLowerCase();
     return ['close','skip','no thanks','dismiss','maybe later','got it'].includes(t)
         || (b.getAttribute('aria-label') || '').toLowerCase() === 'close';
  });
  if (byText) { try { byText.click(); } catch (e) {} }
  ['[class*="tour" i]', '[class*="joyride" i]', '[class*="shepherd" i]',
   '[class*="walkthrough" i]', '[class*="onboarding" i]', '.reactour__mask',
   '[class*="Overlay" i]'].forEach(s => {
     document.querySelectorAll(s).forEach(e => e.remove());
  });
}"""

_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
MAX_PAGES = 25


async def _dismiss(page: Page) -> None:
    try:
        await page.evaluate(_DISMISS_JS)
    except Exception:
        pass


async def _wait_for_results(page: Page, timeout_s: int = 60) -> bool:
    for _ in range(timeout_s // 2):
        await page.wait_for_timeout(2000)
        try:
            title = await page.title()
        except Exception:
            title = ""
        if "Search Results" in title and "Loading" not in title:
            return True
    return False


async def _results_text(page: Page) -> str:
    try:
        return await page.evaluate(
            "() => document.querySelector('#main-content, main, [role=main]')?.innerText "
            "|| document.body.innerText"
        )
    except Exception:
        return ""


def _parse_rows(text: str) -> list[dict]:
    """Split the results innerText into instrument rows.

    Deliberately schema-loose: unlike the lien parser we cannot anchor on a known
    doc-type, because the whole point is to discover which instruments exist. Any
    tab-separated line carrying a date is treated as a row and kept verbatim.
    """
    rows = []
    for raw in (text or "").split("\n"):
        if "\t" not in raw:
            continue
        cells = [c.strip() for c in raw.split("\t") if c.strip()]
        if len(cells) < 3:
            continue
        date = ""
        for c in cells:
            mm = _DATE_RE.search(c)
            if mm:
                date = mm.group(1)
                break
        if not date:
            continue
        inst = next((c for c in cells if re.fullmatch(r"\d{6,}", c)), "")
        upper = " ".join(cells).upper()
        hit = next((k for k in KEY_TYPES if k in upper), "")
        rows.append({"date": date, "instrument": inst, "key_type": hit, "cells": cells})
    return rows


async def search(county: str, name: str, party: str = "both",
                 headed: bool = True) -> list[dict]:
    sub = SUBDOMAINS.get(county.lower())
    if not sub:
        raise SystemExit(f"Unsupported county {county!r}. Options: {list(SUBDOMAINS)}")
    base = f"https://{sub}.tx.publicsearch.us"
    parties = ["grantor", "grantee"] if party == "both" else [party]
    out: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1400, "height": 1300},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        )
        page = await context.new_page()
        page.set_default_timeout(60000)

        for side in parties:
            print(f"\n=== {county} OPR — {side.upper()} = {name!r} (no date limit) ===")
            form_ok = False
            for attempt in range(2):
                await page.goto(f"{base}/search/advanced", wait_until="domcontentloaded")
                await _dismiss(page)
                try:
                    await page.wait_for_selector(f"#{side}-input", timeout=40000)
                    form_ok = True
                    break
                except Exception:
                    print(f"  advanced-search form not ready (attempt {attempt+1}/2)")
                    await page.wait_for_timeout(3000)
            if not form_ok:
                print("  FORM NEVER RENDERED — portal changed or anti-bot blocked us.")
                continue

            await _dismiss(page)
            await page.fill(f"#{side}-input", name)
            await page.wait_for_timeout(400)

            # Exact-text "Search" — "Search Criteria" is a section toggle and a
            # has-text match grabs that instead, silently never submitting.
            clicked = False
            for btn in await page.query_selector_all("button"):
                try:
                    if (await btn.inner_text()).strip().lower() == "search":
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                try:
                    await page.press(f"#{side}-input", "Enter")
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                print("  could not submit the search form")
                continue

            if not await _wait_for_results(page):
                print("  results never loaded (headless anti-bot, or zero hits)")
                continue

            page_no = 1
            while page_no <= MAX_PAGES:
                await _dismiss(page)
                rows = _parse_rows(await _results_text(page))
                for r in rows:
                    r["party_searched"] = side
                out.extend(rows)
                print(f"  page {page_no}: {len(rows)} row(s)")
                try:
                    nxt = page.get_by_role("button", name=re.compile("next page", re.I))
                    if await nxt.count() == 0 or not await nxt.first.is_enabled():
                        break
                    await nxt.first.click()
                    await page.wait_for_timeout(2500)
                    page_no += 1
                except Exception:
                    break

        await browser.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="Name-indexed OPR deed search (GovOS counties)")
    ap.add_argument("--county", required=True, help="Bell | Williamson")
    ap.add_argument("--name", required=True, help='Party name, e.g. "MEDRANO SEBASTIAN"')
    ap.add_argument("--party", default="both", choices=["grantor", "grantee", "both"])
    ap.add_argument("--json", default="", help="Write raw rows to this JSON path")
    ap.add_argument("--headless", action="store_true",
                    help="Force headless (will almost certainly return 0 rows)")
    args = ap.parse_args()

    rows = asyncio.run(search(args.county, args.name, args.party, headed=not args.headless))

    print("\n" + "=" * 92)
    print(f"RESULTS — {args.name} — {len(rows)} instrument(s)")
    print("=" * 92)
    if not rows:
        print("No instruments found. On a pre-digital conveyance this is expected —")
        print("the record is on film/paper at the county clerk. Search the legal")
        print("description in the tract index in person.")
    for r in sorted(rows, key=lambda x: x["date"]):
        star = " *" if r["key_type"] else "  "
        print(f"{star}{r['date']:<12}{r['instrument']:<14}{r['key_type'] or '':<24}"
              f"{' | '.join(r['cells'][:4])}")
    if any(r["key_type"] for r in rows):
        print("\n* = ownership/heirship-relevant instrument")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
        print(f"\nraw rows -> {args.json}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()

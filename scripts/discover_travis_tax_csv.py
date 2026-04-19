"""Discover Travis Tax Office CSV download URLs (v2 — network inspection).

Rather than trying to click the portal's Download button (whose DOM is
shadow-rooted and selector-unfriendly), we open the two public data portals
and dump every outgoing network request + every iframe src. The actual data
lives on data.traviscountytx.gov (Socrata) or on an OpenGov widget URL —
once we know the host + dataset ID, deriving the CSV URL is trivial.

Usage:
    python scripts/discover_travis_tax_csv.py
"""

import asyncio
from playwright.async_api import async_playwright

PORTALS = [
    ("Property Tax Current Year", "https://www.traviscountytx.gov/open-data-portal/property-tax-current-year"),
    ("Delinquent Parcels",        "https://www.traviscountytx.gov/open-records/delinquent-parcels"),
]

INTERESTING_HOSTS = (
    "data.traviscountytx.gov",
    "opengov.com",
    "socrata",
    "/rows.csv",
    "/resource/",
    "/api/views/",
    "accesstype=download",
    ".csv",
)


async def discover() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        all_urls: list[str] = []

        page.on("request", lambda r: all_urls.append(r.url))
        page.on("framenavigated", lambda f: all_urls.append(f"[iframe] {f.url}"))

        for label, url in PORTALS:
            print(f"\n=== {label} ===")
            print(f"Navigating: {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception as e:
                print(f"  goto warning: {e}")
            await asyncio.sleep(5)

            iframes = await page.locator("iframe").all()
            for i, frame in enumerate(iframes):
                src = await frame.get_attribute("src")
                if src:
                    print(f"  [iframe #{i}] {src}")
                    all_urls.append(f"[iframe-attr] {src}")

            hrefs = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
            for h in hrefs:
                if any(tok in h.lower() for tok in INTERESTING_HOSTS):
                    print(f"  [a href] {h}")
                    all_urls.append(f"[a-href] {h}")

        print("\n=== INTERESTING URLs SEEN ===")
        seen: set[str] = set()
        for u in all_urls:
            lower = u.lower()
            if any(tok in lower for tok in INTERESTING_HOSTS) and u not in seen:
                seen.add(u)
                print(f"  {u}")

        if not seen:
            print("  (nothing matched INTERESTING_HOSTS filter)")
            print("\n=== ALL NETWORK URLs (filtered to 3rd-party hosts) ===")
            for u in all_urls:
                if "traviscountytx.gov" not in u and u.startswith(("http", "[")):
                    print(f"  {u}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(discover())

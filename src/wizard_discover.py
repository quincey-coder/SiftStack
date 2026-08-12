"""Read-only discovery walk of the LIVE DataSift upload wizard.

Answers two questions we currently guess at:
  1. How many steps does OUR account's wizard actually have, and in what order?
     (ours is coded for 5; upstream measured 6 after DataSift inserted an
     Enrichment step. Nobody has checked ours.)
  2. What is the real DOM of the COLUMN MAPPING step — the draggable column
     cards and their drop targets, with positions?

Question 2 is the point. ``datasift_uploader`` currently guesses those selectors
and guesses wrong: `div:has-text("Tags")` matches every ANCESTOR div as well as
the card, and the drag helper returns success as soon as it obtains a bounding
box, so it logs "Mapped column: Tags" after dragging a page-level container onto
an arbitrary text node. Selectors written from a real DOM dump instead of from
memory are the fix; this tool produces that dump.

SAFETY — this creates NOTHING in the account:
  * It never clicks "Finish Upload". The list, the tags and the records are all
    committed at Finish; everything before it is staged and discarded. The
    Finish button is explicitly asserted-and-not-clicked at the end of the walk.
  * It uploads a SYNTHETIC 2-row CSV by default, built from our real 72-column
    header, so the mapping screen shows the true column names with no real
    property or owner data leaving the machine.
  * The list name is prefixed ZZ_DISCOVERY_ so that if a future change ever did
    commit one, it is obvious and searchable.

Run (headed by default so you can watch and clear any login challenge):
    python src/wizard_discover.py
    python src/wizard_discover.py --csv output/some_real.csv   # real headers
    python src/wizard_discover.py --headless

Output: output/_wizard/NN_<label>.png + .json, and a summary of the step order.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

import config
from datasift_core import create_browser, login

logger = logging.getLogger(__name__)

OUT = config.OUTPUT_DIR / "_wizard"
RECORDS_URL = "https://app.reisift.io/records/properties"
LIST_NAME = "ZZ_DISCOVERY_DO_NOT_FINISH"
MAX_STEPS = 10

# Header we actually upload with — the mapping screen renders these names.
HEADER_SOURCE = config.PROJECT_ROOT / "output" / "travis_readd_absorbed_2026-08-06.csv"

# Generic page inventory.
DUMP_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = el => (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const cls = el => (el.className || '').toString().slice(0, 70);
  const rect = el => { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const buttons = [...document.querySelectorAll('button, [role=button]')].filter(vis)
    .map(b => ({t: txt(b), disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
                cls: cls(b), rect: rect(b)}));
  const inputs = [...document.querySelectorAll('input, textarea')]
    .map(i => ({type: i.type, ph: i.placeholder, name: i.name, hidden: !vis(i), cls: cls(i)}));
  const selects = [...document.querySelectorAll('[class*="Select"]')].filter(vis)
    .map(s => ({t: txt(s), cls: cls(s), rect: rect(s)})).slice(0, 60);
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,[class*="itle"],[class*="tep"]')]
    .filter(vis).map(h => ({t: txt(h), cls: cls(h)})).slice(0, 40);
  return {url: location.href, buttons, inputs, selects, headings};
}"""

# Mapping-step specific: every LEAF element carrying one of our column names,
# plus anything draggable. Leaf-only is what kills the has-text ancestor bug.
MAPPING_JS = r"""(names) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = el => (el.textContent || '').replace(/\s+/g, ' ').trim();
  const cls = el => (el.className || '').toString().slice(0, 70);
  const rect = el => { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const path = el => { const p = []; let e = el;
    for (let i = 0; i < 5 && e && e.tagName; i++) { p.push(e.tagName.toLowerCase() + (e.className ? '.' + String(e.className).split(' ')[0] : '')); e = e.parentElement; }
    return p.join(' < '); };

  const out = {targets: [], draggables: []};
  const all = [...document.querySelectorAll('*')].filter(vis);

  for (const name of names) {
    for (const el of all) {
      if (txt(el) !== name) continue;               // EXACT text only
      if (el.querySelector('*')) continue;           // LEAF only -> excludes ancestors
      out.targets.push({name, tag: el.tagName.toLowerCase(), cls: cls(el),
                        rect: rect(el), path: path(el),
                        draggable: el.closest('[draggable]') ? el.closest('[draggable]').getAttribute('draggable') : null,
                        parentCls: cls(el.parentElement || el)});
    }
  }
  for (const el of all) {
    const d = el.getAttribute && el.getAttribute('draggable');
    if (d !== null && d !== undefined) out.draggables.push({t: txt(el).slice(0, 60), draggable: d, cls: cls(el), rect: rect(el)});
  }
  out.draggables = out.draggables.slice(0, 120);
  return out;
}"""

# The columns whose mapping we care about — the ones that never auto-map.
COLUMNS_OF_INTEREST = ["Tags", "Lists", "Notes", "Property Street Address"]


def build_discovery_csv(dest: Path, header_source: Path | None = None) -> Path:
    """Synthetic 2-row CSV using our REAL header, so no real data is staged."""
    header = None
    src = header_source or HEADER_SOURCE
    if src.exists():
        with src.open(newline="", encoding="utf-8-sig") as fh:
            header = next(csv.reader(fh))
    if not header:
        header = ["Property Street Address", "Property City", "Property State",
                  "Property Zip", "Owner First Name", "Owner Last Name", "Tags", "Lists"]

    def row(n: int) -> list[str]:
        out = []
        for col in header:
            c = col.strip().lower()
            if c == "property street address":
                out.append(f"{100 + n} Discovery Ln")
            elif c == "property city":
                out.append("Austin")
            elif c == "property state":
                out.append("TX")
            elif c in ("property zip", "property zip code"):
                out.append("78723")
            elif c == "owner first name":
                out.append("Discovery")
            elif c == "owner last name":
                out.append(f"Test{n}")
            elif c == "tags":
                out.append("zz_discovery")
            elif c == "lists":
                out.append("ZZ Discovery")
            else:
                out.append("")
        return out

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerow(row(1))
        w.writerow(row(2))
    logger.info("Discovery CSV: %s (%d columns, 2 synthetic rows)", dest, len(header))
    return dest


def classify_step(info: dict) -> str:
    """Name the screen from the controls present — the step-machine's core idea."""
    btns = " | ".join(b.get("t", "") for b in info.get("buttons", []))
    inputs = info.get("inputs", [])
    phs = " | ".join((i.get("ph") or "") for i in inputs)
    has_file = any(i.get("type") == "file" for i in inputs)
    text = (btns + " " + phs + " " + " ".join(h.get("t", "") for h in info.get("headings", []))).lower()

    # PRECEDENCE MATTERS. Every screen renders the step NAV, so the words
    # "Enrichment", "Add tags" and "Map the columns" appear on ALL of them.
    # Match on controls and on the screen's own HEADING first; generic word
    # matches must come last or the nav mislabels every screen (the first
    # discovery run called the mapping screen "enrichment" for exactly this
    # reason and skipped the DOM capture we were after).
    heads = " | ".join(h.get("t", "") for h in info.get("headings", [])).lower()

    if "finish upload" in btns.lower():
        return "review"
    if "drag the corresponding column" in heads:
        return "map_columns"
    if "search or add a new tag" in phs.lower():
        return "add_tags"
    if has_file:
        return "upload_file"
    if "configure data enrichment" in heads or "property enrichment" in heads:
        return "enrichment"
    if "enter new list name" in phs.lower() or "what are you looking to do" in heads:
        return "setup"
    if "list name" in phs.lower() or "add data" in text:
        return "setup"
    return "unknown"


async def dump(page, index: int, label: str) -> dict:
    await page.wait_for_timeout(1500)
    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"{index:02d}_{label}"
    try:
        await page.screenshot(path=str(OUT / f"{stem}.png"), full_page=True)
    except Exception as exc:
        logger.warning("screenshot %s failed: %s", stem, exc)
    try:
        info = await page.evaluate(DUMP_JS)
    except Exception as exc:
        info = {"error": str(exc)}
    (OUT / f"{stem}.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    logger.info("[%s] url=...%s buttons=%d inputs=%d selects=%d", stem,
                str(info.get("url", ""))[-38:], len(info.get("buttons", [])),
                len(info.get("inputs", [])), len(info.get("selects", [])))
    return info


async def dismiss_popups(page) -> None:
    for sel in ('button:has-text("NO, THANKS")', 'button:has-text("No, thanks")',
                'button:has-text("No, Thanks")'):
        try:
            b = page.locator(sel)
            if await b.count() > 0:
                await b.first.click(timeout=3000)
                await page.wait_for_timeout(400)
        except Exception:
            pass
    # Beamer NPS iframe blocks ALL pointer events globally when present.
    try:
        await page.evaluate(
            "() => { for (const id of ['npsIframeContainer','beamerPushModal']) {"
            " const e = document.getElementById(id); if (e) e.remove(); } }")
    except Exception:
        pass


async def click_next(page) -> bool:
    for sel in ('button:has-text("Next Step")', 'button:has-text("Next")', 'text="Next Step"'):
        b = page.locator(sel)
        if await b.count() > 0:
            try:
                await b.first.click(timeout=8000)
                await page.wait_for_timeout(2800)
                return True
            except Exception:
                continue
    return False


async def fill_setup(page, list_name: str) -> None:
    # The panel opens on "What are you looking to do?" with two BUTTONS
    # (Update Data / Add Data). Until one is chosen the wizard reports
    # "You haven't selected one of the upload options" and keeps Next disabled,
    # which is exactly where the first discovery run stalled.
    add = page.locator('button:has-text("Add Data")')
    if await add.count() > 0:
        await add.first.click()
        await page.wait_for_timeout(1200)

    dd = page.locator('text="Select one option"')
    if await dd.count() > 0:
        await dd.first.click()
        await page.wait_for_timeout(900)
        opt = page.locator('text="Uploading a new list not in DataSift yet"')
        if await opt.count() > 0:
            await opt.first.click()
            await page.wait_for_timeout(900)
    for question, answer in (("WHERE DID YOU PURCHASE THIS LIST?", "Other"),
                             ("DOES DATA CONTAIN PHONE NUMBERS?", "No")):
        try:
            sel = page.locator(f'text="{question}"').locator('..').locator('text="Select an option"')
            if await sel.count() > 0:
                await sel.first.click()
                await page.wait_for_timeout(400)
                o = page.locator(f'text="{answer}"')
                if await o.count() > 0:
                    await o.first.click()
                await page.wait_for_timeout(400)
        except Exception:
            pass
    li = page.locator('input[placeholder*="Enter new list name"], input[placeholder*="list name"]')
    if await li.count() > 0:
        await li.first.fill(list_name)
        await page.wait_for_timeout(400)


async def walk(csv_path: Path, headless: bool) -> list[str]:
    order: list[str] = []
    async with create_browser(headless=headless) as (_browser, _ctx, page):
        ok = await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        logger.info("login=%s url=%s", ok, page.url)
        if not ok:
            logger.error("Login failed — cannot walk the wizard")
            return order
        if "/records" not in page.url:
            await page.goto(RECORDS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        await dismiss_popups(page)

        ub = page.locator('text="Upload File"')
        if await ub.count() == 0:
            await dump(page, 0, "no_upload_button")
            logger.error("'Upload File' not found — is the sidebar rendered?")
            return order
        await ub.first.click()
        await page.wait_for_timeout(3500)
        await dismiss_popups(page)

        file_set = False
        for index in range(1, MAX_STEPS + 1):
            await dismiss_popups(page)
            info = await dump(page, index, "probe")
            step = classify_step(info)
            order.append(step)
            logger.info("  -> step %d identified as: %s", index, step)

            # Re-dump under the identified name so the artifacts are readable.
            await dump(page, index, step)

            if step == "review":
                # HARD STOP. Everything is committed at Finish; we never click it.
                finish = page.locator('button:has-text("Finish Upload")')
                logger.info("REVIEW reached. 'Finish Upload' present=%s — NOT clicking it. "
                            "Nothing was created.", await finish.count() > 0)
                for name in COLUMNS_OF_INTEREST:
                    present = await page.locator(f'text="{name}"').count()
                    logger.info("    review shows %r: %s", name, present > 0)
                break

            if step == "setup":
                await fill_setup(page, LIST_NAME)
                await dump(page, index, "setup_filled")

            if step == "upload_file" and not file_set:
                fi = page.locator('input[type="file"]')
                if await fi.count() > 0:
                    await fi.first.set_input_files(str(csv_path))
                    for _ in range(25):
                        await page.wait_for_timeout(1000)
                        if (await page.locator('text="File uploaded!"').count() > 0
                                or await page.locator('text="100%"').count() > 0):
                            break
                    file_set = True
                    await dump(page, index, "file_uploaded")

            if step == "map_columns":
                # THE PAYLOAD: real geometry for the cards and targets.
                try:
                    mapping = await page.evaluate(MAPPING_JS, COLUMNS_OF_INTEREST)
                except Exception as exc:
                    mapping = {"error": str(exc)}
                # Full inventory of BOTH sides: our unmapped CSV columns and
                # every DataSift field offered as a drop target. This is the
                # table needed to decide which headers to rename vs create.
                try:
                    inventory = await page.evaluate(
                        """() => ({
                             foreign: [...document.querySelectorAll('[class*="UploadModalForeignColumnName"]')]
                                        .map(e => (e.textContent||'').trim()).filter(Boolean),
                             own: [...document.querySelectorAll('[class*="UploadModalOwnColumnName"]')]
                                        .map(e => (e.textContent||'').trim()).filter(Boolean)
                           })""")
                    mapping["inventory"] = inventory
                    logger.info("    CSV columns unmapped (%d): %s",
                                len(inventory["foreign"]), inventory["foreign"])
                    logger.info("    DataSift drop targets (%d): %s",
                                len(inventory["own"]), inventory["own"])
                except Exception as exc:
                    logger.warning("    inventory dump failed: %s", exc)

                (OUT / f"{index:02d}_map_columns_ELEMENTS.json").write_text(
                    json.dumps(mapping, indent=2), encoding="utf-8")
                logger.info("    mapping DOM: %d exact-text leaf node(s), %d draggable(s) -> %s",
                            len(mapping.get("targets", [])), len(mapping.get("draggables", [])),
                            OUT / f"{index:02d}_map_columns_ELEMENTS.json")
                for t in mapping.get("targets", [])[:12]:
                    logger.info("      %-24s x=%-5s y=%-5s %s", t["name"], t["rect"]["x"],
                                t["rect"]["y"], t["cls"][:44])

            if not await click_next(page):
                logger.warning("No clickable Next on step %d (%s) — stopping walk", index, step)
                break

    return order


async def main_async(args) -> int:
    csv_path = Path(args.csv) if args.csv else build_discovery_csv(
        Path(config.OUTPUT_DIR) / "_wizard" / "discovery_2row.csv")
    order = await walk(csv_path, headless=args.headless)

    print("\n" + "=" * 62)
    print("WIZARD STEP ORDER (as rendered on THIS account)")
    print("=" * 62)
    for i, step in enumerate(order, 1):
        print(f"  {i}. {step}")
    print(f"\n  total steps observed: {len(order)}")
    print(f"  our datasift_uploader assumes: 5 (setup, add_tags, upload_file, map_columns, review)")
    print(f"  artifacts: {OUT}")
    print("  NOTHING WAS CREATED — the walk stops before 'Finish Upload'.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", help="CSV to walk with (default: synthetic 2-row, real header)")
    parser.add_argument("--headless", action="store_true",
                        help="no visible browser (default is headed so you can clear challenges)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

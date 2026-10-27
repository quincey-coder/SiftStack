"""Odyssey PublicAccess probate scraper — Williamson County.

Scrapes probate cases from the legacy Odyssey PublicAccess system
(Tyler Technologies, 2003 version). No CAPTCHA, no login required.

Site: https://judicialrecords.wilco.org/PublicAccess/
Search: Search.aspx?ID=200 (Civil, Family & Probate Case Records)
Probate cases: County Court at Law #4 — case numbers END in "-CP4"
("26-1168-CP4"). "24-0468-CP4A" is an ADVERSARY sub-case ("X v. Y") inside
a probate, not an estate — it is admitted only when its title/type says estate.

Flow:
  1. Establish session at default.aspx
  2. Click "Civil, Family & Probate Case Records" menu link
  3. Select "Date Filed" search, set date range
  4. Submit → results grid (all civil/family/probate combined)
  5. Filter to probate cases (-CP4 suffix, "Estate Of" title, or probate type)
  6. Build a notice per estate from the grid row (decedent, case number)
  7. Open each kept case's Register of Actions (same session) and read the
     Party Information block: the Administrator / Executor / Applicant (= the
     PR, our `owner_name`), their retained attorney + phone, and the decedent's
     DOD; plus the Events & Orders list (letters granted, inventory, last
     activity). Only the DOD is taken from the decedent row — the page also
     shows race/height/weight, which are never read.
  8. Return list[NoticeData]; self.last_meta carries returned/kept counters so
     run_health's parse-zero alarm can fire for this scraper.

Travis County and Bell County use the newer Odyssey Portal (2017+) which
has reCAPTCHA — those will be added when a CAPTCHA solution is integrated.
"""

import asyncio
import logging
import random
import re
from datetime import datetime

from playwright.async_api import async_playwright, Page

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

# ── Williamson County config ──────────────────────────────────────────
WILCO_BASE = "https://judicialrecords.wilco.org/PublicAccess"
WILCO_DEFAULT = f"{WILCO_BASE}/default.aspx"
WILCO_SEARCH_ID = 200  # Civil, Family & Probate

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0

# Probate case patterns
# Case numbers ENDING in -CP4 = County Court at Law #4 (Probate Division).
# A trailing letter ("-CP4A") marks an adversary/ancillary sub-case and must
# NOT match on the number alone (it can still qualify by title or type).
_PROBATE_CASE_RE = re.compile(r"-CP4$", re.IGNORECASE)

# "Estate Of NAME, Deceased" pattern
_ESTATE_OF_RE = re.compile(
    r"Estate\s+Of\s+(.+?),\s*Deceased",
    re.IGNORECASE,
)

# Guardianship pattern (also handled by probate court)
_GUARDIANSHIP_RE = re.compile(
    r"Guardianship\s+Of\s+(.+?)(?:,\s*An\s+Incapacitated|$)",
    re.IGNORECASE,
)

# Probate case types we care about
PROBATE_CASE_TYPES = {
    "letters testamentary",
    "independent administration",
    "independent administration w/heirship",
    "dependent administration",
    "temporary administration",
    "muniment of title",
    "small estate affidavit",
    "determination of heirship",
    "probate of will",
}

# ── Register-of-Actions party roles ──────────────────────────────────
# Roles that carry (or will carry, once letters issue) signing authority for
# the estate. Order = priority when a case lists several. "Applicant" is the
# person asking for letters — the PR-to-be — and is the usual role on a fresh
# filing before the order is signed.
_PR_ROLE_PRIORITY = (
    "independent executor", "co-independent executor", "executor", "co-executor",
    "successor executor", "independent administrator", "dependent administrator",
    "successor administrator", "administrator", "temporary administrator",
    "applicant", "petitioner",
)
# Never treated as the PR even though they appear in the party block.
_NON_PR_ROLE_RE = re.compile(
    r"decedent|heir|ward|guardian|ad litem|attorney|interested|creditor|claimant|respondent|defendant",
    re.IGNORECASE,
)
_DOD_RE = re.compile(r"DOD:\s*(\d{1,2}/\d{1,2}/\d{4})")
_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_ATTORNEY_STATUS_RE = re.compile(
    r"\s*(Retained|Court Appointed|Appointed|Pro Se|Lead)\b.*$", re.IGNORECASE | re.DOTALL,
)
_AKA_SPLIT_RE = re.compile(r"\s+(?:Also Known As|a/k/a|aka)\s+", re.IGNORECASE)
_EVENT_LINE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})\t(.*)$")
_DATE_ONLY_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})\s*$")
_LETTERS_RE = re.compile(r"\bletters\b", re.IGNORECASE)
_LETTERS_SKIP_RE = re.compile(r"application|citation|notice|motion|due", re.IGNORECASE)
_INVENTORY_RE = re.compile(r"\binventory\b", re.IGNORECASE)

# JS: structured party rows from the "Party Information" table + body text.
# PublicAccess renders role and name as consecutive <th class="ssTableHeader">
# cells; attorney / DOD / demographics live in the following <td>s.
_CASE_DETAIL_JS = """() => {
  const parties = [];
  const seen = new Set();
  document.querySelectorAll('table').forEach(t => {
    const txt = t.innerText || '';
    if (!/Party Information/.test(txt) || /Events & Orders/.test(txt)) return;
    t.querySelectorAll('tr').forEach(tr => {
      const ths = tr.querySelectorAll('th.ssTableHeader');
      if (ths.length < 2) return;
      const role = ths[0].innerText.trim();
      const name = ths[1].innerText.trim();
      const details = Array.from(tr.querySelectorAll('td'))
        .map(c => c.innerText.trim()).filter(Boolean);
      const key = role + '|' + name;
      if (seen.has(key)) return;
      seen.add(key);
      parties.push({role: role, name: name, details: details});
    });
  });
  return {parties: parties, body: document.body.innerText};
}"""


def _is_probate_case(case_number: str, case_title: str, case_type: str) -> bool:
    """Check if a case is a probate case we want to scrape."""
    # -CP4 suffix = County Court at Law #4 (Probate). "-CP4A" (adversary
    # sub-case) does not match here and must qualify on title/type.
    if _PROBATE_CASE_RE.search((case_number or "").strip()):
        return True
    # "Estate Of ... Deceased" in title
    if _ESTATE_OF_RE.search(case_title or ""):
        return True
    # Probate case type
    if (case_type or "").lower().strip() in PROBATE_CASE_TYPES:
        return True
    return False


def _extract_decedent_name(case_title: str) -> str:
    """Extract decedent name from 'Estate Of NAME, Deceased'."""
    m = _ESTATE_OF_RE.search(case_title)
    if m:
        name = " ".join(m.group(1).split())
        if name.isupper():
            name = name.title()
        return name
    return ""


def _clean_cell(text: str) -> str:
    """Collapse the \\xa0 / tab / replacement-character noise of innerText."""
    text = (text or "").replace("\xa0", " ").replace("�", " ")
    return " ".join(text.split())


_NAME_SUFFIX_RE = re.compile(r"^(jr|sr|ii|iii|iv|v|esq)\.?$", re.IGNORECASE)
_ENTITY_PARTY_RE = re.compile(
    r"\b(bank|trust|llc|l\.l\.c|inc|corp|corporation|company|co\.|n\.a\.|association|"
    r"funding|capital|services|partners|lp|ltd|foundation|church|county|city of|state of)\b",
    re.IGNORECASE,
)


def party_name_to_first_last(raw: str) -> str:
    """'Verock, Tawanna Kay' → 'Tawanna Kay Verock'; 'STILES, JASON MICHAEL' →
    'Jason Michael Stiles'; 'Smith, John, Jr.' → 'John Smith Jr'. The comma makes
    the orientation unambiguous, so this never guesses (unlike
    normalize_court_name). Aliases after "Also Known As" are dropped. Entity
    parties ('Wells Fargo Bank, N.A.') are returned as written — never flipped.
    Returns "" for placeholders (**Confidential**, Unknown Heirs)."""
    name = _clean_cell(raw)
    if not name or name.startswith("**") or "confidential" in name.lower():
        return ""
    if re.search(r"\bunknown\b", name, re.IGNORECASE):
        return ""
    name = _AKA_SPLIT_RE.split(name, maxsplit=1)[0].strip()
    if _ENTITY_PARTY_RE.search(name):
        return " ".join(name.split())
    if "," in name:
        parts = [p.strip() for p in name.split(",") if p.strip()]
        last, rest = parts[0], parts[1:]
        # A trailing ", Jr." / ", III" is a suffix, not a second given name.
        suffix = ""
        if rest and _NAME_SUFFIX_RE.match(rest[-1]):
            suffix = rest.pop().rstrip(".")
        given = " ".join(rest)
        name = f"{given} {last}".strip()
        if suffix:
            name = f"{name} {suffix}"
    if name.isupper():
        name = name.title()
    return " ".join(name.split())


def _pr_role_rank(role: str) -> int | None:
    """Priority index for a party role, or None when the role is not a PR."""
    r = _clean_cell(role).lower()
    if not r or _NON_PR_ROLE_RE.search(r):
        return None
    for i, key in enumerate(_PR_ROLE_PRIORITY):
        if key in r:
            return i
    return None


def _parse_attorney(details: list[str]) -> tuple[str, str]:
    """('Ted W Hejl', '512-365-6348') from the attorney cell of a party row."""
    for cell in details:
        c = _clean_cell(cell)
        if not re.search(r"Retained|Court Appointed|Appointed|Pro Se", c, re.IGNORECASE) \
                and not _PHONE_RE.search(c):
            continue
        phone_m = _PHONE_RE.search(c)
        phone = phone_m.group(0) if phone_m else ""
        name = _ATTORNEY_STATUS_RE.sub("", c).strip()
        if phone and phone in name:
            name = name.replace(phone, "").strip()
        name = re.sub(r"\(W\)|\(H\)|\(C\)", "", name).strip(" -")
        if name and not _DOD_RE.search(c):
            return name, phone
    return "", ""


def _parse_events(body_text: str) -> list[tuple[str, str]]:
    """[(ISO date, event text), ...] from the Events & Orders section."""
    text = body_text or ""
    i = text.find("Events & Orders")
    if i < 0:
        return []
    text = text[i:]
    # Stop before the Financial Information table some case pages append —
    # its "MM/DD/YYYY\tTransaction Assessment\t$400.00" rows would otherwise
    # count as docket events (and a fee line mentioning "Letters" could set
    # letters_date).
    j = text.find("Financial Information")
    if j > 0:
        text = text[:j]
    lines = text.split("\n")
    events: list[tuple[str, str]] = []

    def _iso(d: str) -> str:
        try:
            return datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return ""

    for k, line in enumerate(lines):
        m = _EVENT_LINE_RE.match(line)
        if m:
            txt = _clean_cell(m.group(2))
            if txt:
                events.append((_iso(m.group(1)), txt))
            continue
        dm = _DATE_ONLY_RE.match(line)
        if dm:
            # Dispositions render as the date on its own line, then the order
            # text on the next non-empty line.
            for nxt in lines[k + 1:k + 3]:
                txt = _clean_cell(nxt)
                if txt:
                    events.append((_iso(dm.group(1)), txt))
                    break
    return [(d, t) for d, t in events if d]


def parse_case_detail(parties: list[dict], body_text: str) -> dict:
    """Turn the structured party rows + page text into the fields we keep.

    Returns keys: case_number, pr_name, pr_role, attorney_name, attorney_phone,
    date_of_death (ISO), decedent_name, events, letters_date, inventory_date,
    last_event_date, dispositions (list[str]).
    """
    out = {
        "case_number": "", "pr_name": "", "pr_role": "", "attorney_name": "",
        "attorney_phone": "", "date_of_death": "", "decedent_name": "",
        "events": [], "letters_date": "", "inventory_date": "",
        "last_event_date": "", "dispositions": [],
    }
    m = re.search(r"Case No\.\s*([A-Z0-9-]+)", body_text or "")
    if m:
        out["case_number"] = m.group(1).strip()

    best: tuple[int, dict] | None = None
    for p in parties or []:
        role = _clean_cell(p.get("role", ""))
        details = [str(d) for d in (p.get("details") or [])]
        if role.lower().startswith("decedent"):
            if not out["decedent_name"]:
                out["decedent_name"] = party_name_to_first_last(p.get("name", ""))
            for cell in details:
                dm = _DOD_RE.search(_clean_cell(cell))
                if dm:
                    try:
                        out["date_of_death"] = datetime.strptime(
                            dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                    break
            continue
        rank = _pr_role_rank(role)
        if rank is None:
            continue
        name = party_name_to_first_last(p.get("name", ""))
        if not name:
            continue
        if best is None or rank < best[0]:
            best = (rank, {"name": name, "role": role, "details": details})
    if best:
        out["pr_name"] = best[1]["name"]
        out["pr_role"] = best[1]["role"]
        out["attorney_name"], out["attorney_phone"] = _parse_attorney(best[1]["details"])

    events = _parse_events(body_text)
    out["events"] = events
    if events:
        out["last_event_date"] = max(d for d, _ in events)
        for d, t in events:
            if _LETTERS_RE.search(t) and not _LETTERS_SKIP_RE.search(t) and not out["letters_date"]:
                out["letters_date"] = d
            if _INVENTORY_RE.search(t) and not re.search(r"\bdue\b", t, re.IGNORECASE) \
                    and not out["inventory_date"]:
                out["inventory_date"] = d
        # Dispositions = orders; keep any event text that starts with "Order"
        out["dispositions"] = [t for _, t in events if t.lower().startswith("order")]
    return out


def apply_case_detail(notice: NoticeData, detail: dict) -> None:
    """Copy the parsed Register-of-Actions fields onto the notice.

    owner_name = the PR (court-named Administrator/Executor, or the Applicant
    for letters) in FIRST LAST form — the probate domain rule. The decedent
    stays in decedent_name. DOD → date_of_death + owner_deceased. Attorney and
    docket activity go to raw_text (Notes) — there is no NoticeData field for
    them and a custom field is owner-gated.
    """
    if detail.get("case_number") and not notice.case_number:
        notice.case_number = detail["case_number"]
    if detail.get("date_of_death"):
        notice.date_of_death = detail["date_of_death"]
        notice.owner_deceased = "yes"
    if not notice.decedent_name and detail.get("decedent_name"):
        notice.decedent_name = detail["decedent_name"]
    pr = detail.get("pr_name", "")
    if pr:
        notice.owner_name = pr
        role_l = detail.get("pr_role", "").lower()
        if "executor" in role_l:
            rel = "executor"
        elif "administrator" in role_l:
            rel = "administrator"
        else:
            rel = "applicant"
        if not notice.decision_maker_name:
            notice.decision_maker_name = pr
            notice.decision_maker_relationship = rel
            notice.decision_maker_source = "court_record"
    lines = []
    if pr:
        lines.append(f"PR: {pr} ({detail.get('pr_role', '')})")
    if detail.get("attorney_name"):
        att = detail["attorney_name"]
        if detail.get("attorney_phone"):
            att += f" {detail['attorney_phone']}"
        notice.case_attorney = att           # → Notes via datasift_formatter
        lines.append(f"Attorney: {att}")
    if detail.get("date_of_death"):
        lines.append(f"DOD: {detail['date_of_death']}")
    ev = detail.get("events") or []
    if ev:
        docket = f"{len(ev)} events, last {detail.get('last_event_date', '')}"
        if detail.get("letters_date"):
            docket += f"; Letters: {detail['letters_date']}"
        if detail.get("inventory_date"):
            docket += f"; Inventory: {detail['inventory_date']}"
        notice.case_docket = docket          # → Notes via datasift_formatter
        lines.append(f"Docket: {docket}")
    if lines:
        # raw_text is for logs/debugging; Notes are built from the fields above.
        notice.raw_text = (notice.raw_text + "\n" if notice.raw_text else "") + "\n".join(lines)


async def _establish_session(page: Page, base_url: str) -> None:
    """Navigate to the default page to establish an ASP.NET session."""
    await page.goto(f"{base_url}/default.aspx", wait_until="networkidle")
    await page.wait_for_timeout(1500)
    logger.info("Session established at %s", base_url)


async def _navigate_to_search(page: Page, base_url: str, search_id: int) -> None:
    """Navigate to the case search page via menu link."""
    # Click the appropriate menu link
    if search_id == 200:
        link = page.locator('a:has-text("Civil, Family & Probate")')
    else:
        link = page.locator(f'a[href*="ID={search_id}"]')

    if await link.count() > 0:
        await link.first.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)
    else:
        # Fallback: direct navigation (may lose session)
        await page.evaluate(f'window.location.href = "Search.aspx?ID={search_id}"')
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)


async def _submit_date_search(
    page: Page, from_date: datetime, to_date: datetime
) -> int:
    """Select Date Filed search, fill dates, submit. Returns record count."""
    # Select "Date Filed" radio
    await page.check("#DateFiled")
    await page.wait_for_timeout(300)

    # Fill date range (MM/DD/YYYY format)
    from_str = from_date.strftime("%m/%d/%Y")
    to_str = to_date.strftime("%m/%d/%Y")
    await page.fill("#DateFiledOnAfter", from_str)
    await page.fill("#DateFiledOnBefore", to_str)

    logger.info("Date range: %s to %s", from_str, to_str)

    # Submit
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        await page.click("#SearchSubmit")
    await page.wait_for_timeout(2000)

    # Extract record count from "Record Count: NNN"
    body = await page.inner_text("body")
    count_match = re.search(r"Record\s+Count:\s*(\d+)", body)
    total = int(count_match.group(1)) if count_match else 0
    logger.info("Search returned %d total civil/family/probate records", total)
    return total


async def _parse_results_page(page: Page) -> list[dict]:
    """Parse case rows from the current results page using JS cell extraction.

    PublicAccess grid has 4 cells per row:
      cell[0]: Case number (e.g., "26-0407-CP4")
      cell[1]: Title (e.g., "Estate Of\\nDanita Joyce Futch,\\nDeceased")
      cell[2]: Date + Court merged (e.g., "04/01/2026County Court at Law #4")
      cell[3]: Case type + Status merged (e.g., "Letters TestamentaryFiled")

    Returns list of dicts; `is_probate` marks the ones we keep.
    """
    # Extract all rows with CaseDetail links via JS (much faster than Playwright locators)
    raw_rows = await page.evaluate("""
        (() => {
            var rows = document.querySelectorAll('tr');
            var results = [];
            rows.forEach(row => {
                var link = row.querySelector('a[href*="CaseDetail"]');
                if (!link) return;
                var cells = row.querySelectorAll('td');
                var cellTexts = [];
                cells.forEach(c => cellTexts.push(c.textContent.trim()));
                results.push({
                    caseNum: link.textContent.trim(),
                    href: link.getAttribute('href') || '',
                    cells: cellTexts
                });
            });
            return results;
        })()
    """)

    records = []
    for raw in raw_rows:
        cells = raw.get("cells", [])
        if len(cells) < 4:
            continue

        case_number = raw["caseNum"]
        case_title = cells[1] if len(cells) > 1 else ""

        # Parse date from cell[2] — format "MM/DD/YYYYCourt Name"
        date_court = cells[2] if len(cells) > 2 else ""
        filed_date = ""
        court = ""
        date_match = re.match(r"(\d{2}/\d{2}/\d{4})(.*)", date_court)
        if date_match:
            filed_date = date_match.group(1)
            court = date_match.group(2).strip()

        # Parse case type from cell[3] — format "Case TypeStatus"
        type_status = cells[3] if len(cells) > 3 else ""
        case_type = re.sub(r"(?:Filed|Closed|Pending|Disposed|Active)\s*$", "", type_status).strip()

        is_probate = _is_probate_case(case_number, case_title, case_type)

        records.append({
            "case_number": case_number,
            "case_title": case_title,
            "filed_date": filed_date,
            "court": court,
            "case_type": case_type,
            "detail_url": raw["href"],
            "is_probate": is_probate,
        })

    return records


async def _fetch_case_detail(page: Page, base_url: str, detail_url: str) -> dict:
    """Open a case's Register of Actions (session already armed by the search)
    and return parse_case_detail()'s dict. Raises on navigation failure."""
    await page.goto(f"{base_url}/{detail_url}", wait_until="domcontentloaded")
    await page.wait_for_timeout(500)
    data = await page.evaluate(_CASE_DETAIL_JS)
    body = (data or {}).get("body", "") or ""
    if "Register of Actions" not in body:
        raise RuntimeError("case page did not render a Register of Actions")
    return parse_case_detail((data or {}).get("parties", []), body)


def case_url(base_url: str, detail_url: str, case_number: str = "") -> str:
    """A WORKING link for a Williamson case, with the CaseID in the fragment.

    `CaseDetail.aspx?CaseID=N` only renders after a search in the same ASP.NET
    session — a bare GET shows "Public Access Error" (verified live 2026-10-27),
    so that link was dead for anyone reading the CRM. The portal landing page
    always opens; the fragment keeps the CaseID machine-readable (same id as
    before in data_formatter._notice_id_from_url) and carries the human case
    number for the "Case" search box. Never use `Search.aspx?ID=200` here: the
    legacy `[?&]ID=` dedup regex would key every record on "200".
    """
    m = re.search(r"CaseID=(\d+)", detail_url or "", re.IGNORECASE)
    if not m:
        return f"{base_url}/{detail_url}" if detail_url else base_url
    frag = f"CaseID={m.group(1)}"
    if case_number:
        frag += f"&case={case_number.strip()}"
    return f"{base_url}/default.aspx#{frag}"


def _build_notice(record: dict, base_url: str, county: str) -> NoticeData | None:
    """Build a NoticeData from a probate case grid record.

    Extracts the decedent name and case number from the grid row. The PR /
    executor, attorney and DOD come from the case page (apply_case_detail);
    when that page cannot be read, enrichment's obituary step still fills them.
    """
    notice = NoticeData(
        notice_type="probate",
        county=county,
        state="TX",
        source_url=case_url(base_url, record.get("detail_url", ""), record.get("case_number", "")),
        case_number=(record.get("case_number", "") or "").strip(),
    )

    # Date filed
    filed_date = record.get("filed_date", "")
    if filed_date:
        try:
            dt = datetime.strptime(filed_date, "%m/%d/%Y")
            notice.date_added = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Decedent name from title ("Estate Of NAME, Deceased")
    case_title = record.get("case_title", "")
    decedent = _extract_decedent_name(case_title)
    if decedent:
        # Odyssey names are already FIRST LAST — don't flip; just split off any
        # "a/k/a" alias so it doesn't pollute the name and can be searched.
        from notice_parser import _split_aka
        primary, alias = _split_aka(decedent)
        notice.decedent_name = primary
        if alias:
            notice.decedent_aka = alias
    elif _GUARDIANSHIP_RE.search(case_title):
        # Skip guardianship cases — we only want estate/probate
        return None

    # Store case info as raw text for enrichment pipeline
    notice.raw_text = (
        f"Case: {record.get('case_number', '')}\n"
        f"Title: {' '.join(case_title.split())}\n"
        f"Type: {record.get('case_type', '')}\n"
        f"Court: {record.get('court', '')}\n"
        f"Filed: {filed_date}"
    )

    # Only return if we got a decedent name
    if not notice.decedent_name:
        return None

    return notice


@register("Williamson", "probate")
class WilliamsonProbateScraper:
    """Scrape probate cases from Williamson County Odyssey PublicAccess."""

    def __init__(self, fetch_details: bool = True):
        self.fetch_details = fetch_details
        self.last_meta: dict = {}

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        from scrapers.lookback import daily_window

        # Rolling lookback: daily runs re-scan at least the last 14 days so
        # filings the clerk completes late are re-hit (dedup absorbs repeats).
        from_date, to_date, _ = daily_window(
            mode, since_date, log_label="Williamson probate",
        )

        logger.info(
            "Williamson probate scrape: mode=%s, range=%s to %s",
            mode, from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d"),
        )

        all_notices: list[NoticeData] = []
        probate_records: list[dict] = []
        # run_health reads returned/kept: "returned>0, kept==0" is the ERROR-level
        # parse-zero alarm, so `returned` must be the rows the PARSER was asked
        # to keep (probate candidates), not every civil/family row the portal
        # listed — otherwise a window with no estates (a 1-day smoke) would read
        # as a grid regression. The all-rows total is kept as `grid_rows`.
        self.last_meta = {
            "returned": 0, "grid_rows": 0, "probate_candidates": 0, "kept": 0,
            "dropped_no_decedent": 0, "detail_ok": 0, "detail_failed": 0,
            "window_days": (to_date - from_date).days + 1,
        }

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )
            page = await context.new_page()
            page.set_default_timeout(30000)

            try:
                # Step 1: Establish session
                await _establish_session(page, WILCO_BASE)

                # Step 2: Navigate to Civil/Family/Probate search
                await _navigate_to_search(page, WILCO_BASE, WILCO_SEARCH_ID)

                # Step 3: Submit date-based search
                total = await _submit_date_search(page, from_date, to_date)
                self.last_meta["grid_rows"] = total
                if total == 0:
                    return []

                # Step 4: Parse results and filter to probate cases
                all_records = await _parse_results_page(page)
                probate_records = [r for r in all_records if r["is_probate"]]
                self.last_meta["probate_candidates"] = len(probate_records)
                self.last_meta["returned"] = len(probate_records)

                logger.info(
                    "Found %d probate cases out of %d total records",
                    len(probate_records), len(all_records),
                )

                if not probate_records:
                    return []

                # Step 5: Build notices from grid data
                pending: list[tuple[NoticeData, dict]] = []
                for record in probate_records:
                    if max_notices and len(pending) >= max_notices:
                        break
                    notice = _build_notice(record, WILCO_BASE, "Williamson")
                    if notice:
                        pending.append((notice, record))
                    else:
                        self.last_meta["dropped_no_decedent"] += 1

                # Step 6: Register of Actions per kept case (PR, attorney, DOD,
                # docket). One failure never drops the grid record.
                for notice, record in pending:
                    if self.fetch_details and record.get("detail_url"):
                        try:
                            await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                            detail = await _fetch_case_detail(page, WILCO_BASE, record["detail_url"])
                            apply_case_detail(notice, detail)
                            self.last_meta["detail_ok"] += 1
                        except Exception as e:  # noqa: BLE001 — per-case, non-fatal
                            self.last_meta["detail_failed"] += 1
                            logger.warning(
                                "Williamson probate %s: case page not read (%s); "
                                "keeping grid record", record.get("case_number"), e,
                            )
                    all_notices.append(notice)

            except Exception as e:
                # Fail LOUD (run_health records a hard fail) but hand back the
                # notices already built so partial results still flow.
                from scrapers import ScraperError
                logger.error("Williamson probate scraper failed: %s", e)
                if isinstance(e, ScraperError):
                    e.partial = all_notices
                    raise
                raise ScraperError(f"Williamson probate: {e}", partial=all_notices) from e
            finally:
                await browser.close()

        self.last_meta["kept"] = len(all_notices)
        logger.info(
            "Williamson probate scrape complete: %d notices from %d probate cases "
            "(case pages read: %d, failed: %d)",
            len(all_notices), len(probate_records),
            self.last_meta["detail_ok"], self.last_meta["detail_failed"],
        )
        return all_notices

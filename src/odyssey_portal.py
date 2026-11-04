"""Travis + Bell court dockets by party name (Tyler Odyssey Portal "Smart Search").

The curative audit's docket stage, for the two counties whose court records sit
on the newer Odyssey Portal (Williamson's legacy PublicAccess has its own path in
curative_audit._probate_search). Results come back in the Williamson cache shape,
``{name: {"cases": [estate cases], "other": [civil cases]}}``, so grade_probate /
grade_court_cases read all three counties the same way.

Verified live 2026-10-29:

- Travis (odysseyweb.traviscountytx.gov/Portal) asks for a reCAPTCHA v2 on EVERY
  Smart Search submit (``Settings.CaptchaEnabled``). 2Captcha solves it in
  seconds (owner-approved spend, ~$0.003 a search). The Register of Actions
  opens in the same session with no further captcha.
- Bell (justice.bellcounty.texas.gov/PublicPortal - NOT ``www.``, which no longer
  resolves) has no captcha at all.
- Search text is "Last, First". Results are a party grid (name + DOB; Bell masks
  it to XX/XX/YYYY) with each party's cases nested: number, style (case caption),
  type, status, court. Anonymous users never see the case parties, so the PR /
  attorney are not available here the way they are in Williamson.
- Travis probate captions are often just the decedent's name ("ELEANOR JO SMITH");
  estate cases are recognised by the case type, and the caption is rewritten to
  "Estate Of <party>" so _case_decedent can read it.

Name matching here is deliberately loose (first + last); the curative audit
grades every hit against the deeds, the middle initial and the owner's age.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PORTALS = {
    "Travis": "https://odysseyweb.traviscountytx.gov/Portal/",
    "Bell": "https://justice.bellcounty.texas.gov/PublicPortal/",
}
MAX_SOLVES = int(os.getenv("ODYSSEY_MAX_CAPTCHA_SOLVES", "6000"))   # ~$18 at 2Captcha reCAPTCHA v2 rates
MAX_DETAILS = 4   # Register of Actions pages opened per name searched

_ESTATE_TYPE_RE = re.compile(r"ESTATE|ADMINISTRATION|TESTAMENT|HEIRSHIP|MUNIMENT|PROBATE|\bWILL\b", re.I)
# "Civil Action (Probate Civil Action/ Ancilliary/736)" is a Rule 736 home-equity foreclosure, not an estate
_NOT_ESTATE_RE = re.compile(r"GUARDIAN|MENTAL|COMMITMENT|TRUST\b|CIVIL ACTION|\b736\b", re.I)
_OTHER_TYPE_RE = re.compile(r"TAX|DIVORCE|MARRIAGE|PARTITION|TRESPASS|QUIET|TITLE|FORECLOS|HOME EQUITY|LIEN|\b736\b",
                            re.I)
_SKIP_TYPE_RE = re.compile(r"TRAFFIC|FORCIBLE|EVICTION|TITLE IV|PROTECTIVE|MISDEMEANOR|FELONY|DWI|THEFT|ASSAULT",
                           re.I)


_ROLE_RE = re.compile(r"Plaintiff|Defendant|Applicant|Petitioner|Respondent|Decedent|Deceased|Executor|Executrix|"
                      r"Administrat\w+|Independent \w+|Temporary \w+|Heir|Distributee|Beneficiary|Proponent|"
                      r"Intervenor|Claimant|Contestant|Guardian|Ward|Proposed Ward", re.I)
_LABELS = {"Case Type", "Case Status", "Court", "Judicial Officer", "Case Number", "Return Unserved",
           "Anticipated Server", "Anticipated Method", "Comment", "Served", "Returned"}
_PR_ROLE_RE =re.compile(r"EXECUT|ADMINISTRAT|APPLICANT|PETITIONER|PROPONENT", re.I)


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _iso(mdy: str) -> str:
    try:
        return datetime.strptime(mdy.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _first_last(name: str) -> tuple[str, str]:
    """'Smith, Eleanor A' -> ('ELEANOR', 'SMITH'); 'Eleanor A Smith' -> same."""
    name = _ws(name).upper()
    if "," in name:
        last, rest = name.split(",", 1)
        toks = re.findall(r"[A-Z']+", rest)
        return (toks[0] if toks else ""), _ws(last).split(" ")[-1] if last else ""
    toks = [t for t in re.findall(r"[A-Z']+", name) if t not in ("JR", "SR", "II", "III", "IV")]
    return (toks[0], toks[-1]) if len(toks) >= 2 else ("", "")


def display_name(party: str) -> str:
    """'SMITH, ELEANOR A' -> 'Eleanor A Smith'."""
    if "," not in party:
        return _ws(party).title()
    last, rest = party.split(",", 1)
    suf = re.search(r",?\s*\b(JR|SR|II|III|IV)\.?\s*$", rest, re.I)
    if suf:
        rest = rest[:suf.start()]
    name = _ws(f"{rest.replace(',', ' ')} {last} {suf.group(1) if suf else ''}").title()
    return re.sub(r"\b(Ii|Iii|Iv)$", lambda m: m.group(1).upper(), name)


def parse_results(html: str) -> list[dict]:
    """Party grid -> [{name, dob, cases: [{case_number, style, type, status, location, url}]}]."""
    soup = BeautifulSoup(html, "html.parser")
    parties = []
    for a in soup.select("a.nolink[data-party-id]"):
        pid = a.get("data-party-id")
        row = a.find_parent("tr")
        cells = row.find_all("td") if row else []
        dob = _ws(cells[-1].get_text()) if len(cells) >= 2 else ""
        card = soup.select_one(f'.party-card[data-party-id="{pid}"]')
        cases = []
        for tr in (card.select("tr") if card else []):
            link = tr.select_one("td.party-case-caseid a.caseLink")
            if not link:
                continue
            style_div = tr.select_one("td.party-case-style div")

            def cell(cls: str) -> str:
                td = tr.select_one(f"td.{cls}")
                return _ws(td.get_text()) if td else ""
            cases.append({"case_number": _ws(link.get_text()),
                          "style": _ws(style_div.get("title") if style_div else ""),
                          "type": cell("party-case-type"), "status": cell("party-case-status"),
                          "location": cell("party-case-location"), "url": link.get("data-url") or ""})
        parties.append({"name": _ws(a.get_text()), "dob": dob, "cases": cases})
    return parties


def parse_detail(text: str) -> dict:
    """Register of Actions text -> filed, style, subtype, status and the events that matter."""
    lines = [_ws(x) for x in (text or "").splitlines() if _ws(x)]
    out: dict = {}
    for i, ln in enumerate(lines):
        if ln.startswith("Case No.") and i + 1 < len(lines) and "style" not in out:
            out["style"] = lines[i + 1]
        elif ln == "Filed on:" and i + 1 < len(lines):
            out["filed"] = _iso(lines[i + 1])
        elif ln.startswith("Filed on:") and len(ln) > 10:
            out["filed"] = _iso(ln.split(":", 1)[1])
        elif re.match(r"Case.?Type:", ln):
            out["case_type"] = _ws(ln.split(":", 1)[1])
        elif ln.startswith("Subtype:"):
            out["subtype"] = _ws(ln.split(":", 1)[1])
        elif re.match(r"Case.?Status:", ln) and i + 1 < len(lines):
            out["status"] = _ws(ln.split(":", 1)[1]) or lines[i + 1]
        # Bell's CaseDetail layout: bare labels with the value on the next line
        elif ln in ("File Date", "Case Type", "Case Status") and i + 1 < len(lines):
            key = {"File Date": "filed", "Case Type": "case_type", "Case Status": "status"}[ln]
            out.setdefault(key, _iso(lines[i + 1]) if key == "filed" else lines[i + 1])
        elif re.match(r"^\S+ \| ", ln) and "style" not in out:
            out["style"] = ln.split(" | ", 1)[1]
    events = []
    for i, ln in enumerate(lines):
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", ln) and i + 1 < len(lines) and \
                not re.fullmatch(r"\d{2}/\d{2}/\d{4}", lines[i + 1]) and \
                lines[i - 1] not in ("File Date", "Filed on:") and lines[i + 1] not in _LABELS:
            events.append((_iso(ln), lines[i + 1]))
        elif m := re.match(r"^(\d{2}/\d{2}/\d{4}) (.+)$", ln):
            events.append((_iso(m.group(1)), m.group(2)))
    # parties (Bell shows them; Travis hides them from anonymous users)
    parties, attorneys = [], []
    # "Party" also appears in the print menus; the real section is followed by a role
    start = next((i for i, ln in enumerate(lines[:-1]) if ln == "Party" and _ROLE_RE.fullmatch(lines[i + 1])), None)
    if start is not None:
        end = next((i for i in range(start, len(lines)) if lines[i] == "Events and Hearings"), len(lines))
        seg = lines[start + 1:end]
        for i, ln in enumerate(seg[:-1]):
            if _ROLE_RE.fullmatch(ln):
                parties.append({"role": ln, "name": display_name(seg[i + 1])})
            elif ln == "Lead Attorney":
                attorneys.append(display_name(seg[i + 1]))
    out["parties"] = parties
    out["pr_name"] = next((p["name"] for p in parties if _PR_ROLE_RE.search(p["role"])), "")
    out["pr_role"] = next((p["role"] for p in parties if _PR_ROLE_RE.search(p["role"])), "")
    out["attorney_name"] = attorneys[0] if attorneys else ""
    out["events"] = [f"{d} {t}" for d, t in events][-40:]

    def first(pat: str) -> str:
        return next((d for d, t in events if re.search(pat, t, re.I)), "")
    out["letters_date"] = first(r"LETTERS? (TESTAMENTARY|OF (TEMPORARY |DEPENDENT |INDEPENDENT )?ADMIN)")
    out["inventory_date"] = first(r"INVENTORY")
    out["heirship_date"] = first(r"JUDGMENT.*HEIRSHIP|DECLARING HEIRSHIP|HEIRSHIP.*JUDGMENT")
    out["sale_date"] = first(r"ORDER.*SALE|SALE OF REAL|REPORT OF SALE")
    out["last_event_date"] = max((d for d, _ in events if d), default="")
    return out


def classify(parties: list[dict], first: str, last: str) -> dict:
    """Keep the cases of parties named first+last -> {"cases": estates, "other": civil}."""
    f, l_ = first.upper(), last.upper()
    estates, other, seen = [], [], set()
    for p in parties:
        pf, pl = _first_last(p["name"])
        # 'GARCIA LOPEZ, MARIA' is found by a search for Garcia: match any surname token before the comma
        surnames = set(re.findall(r"[A-Z']+", p["name"].upper().split(",")[0])) if "," in p["name"] else {pl}
        if pf != f or l_ not in surnames:
            continue
        person = display_name(p["name"])
        by = re.search(r"(\d{4})\s*$", p.get("dob") or "")
        for c in p["cases"]:
            if c["case_number"] in seen:
                continue
            typ = c["type"]
            if _SKIP_TYPE_RE.search(typ):
                continue
            base = {"case_number": c["case_number"], "type_status": f"{typ} / {c['status']}",
                    "location": c["location"], "party": person, "party_birth_year": int(by.group(1)) if by else None,
                    "url": c["url"], "filed": ""}
            if _ESTATE_TYPE_RE.search(typ) and not _NOT_ESTATE_RE.search(typ):
                style = c["style"]
                if not re.search(r"ESTATE OF", style, re.I):
                    # Travis titles an estate with the DECEDENT's name, LAST FIRST ('NICHOLS
                    # ELIZABETH'); the searched party is often the applicant/heir, not the decedent
                    named = {f, l_} <= set(re.findall(r"[A-Z']+", style.upper()))
                    style = f"Estate Of {person if named else _style_decedent(style, l_)}"
                seen.add(c["case_number"])
                estates.append({**base, "style": _estate_style(style), "style_raw": c["style"]})
            elif _OTHER_TYPE_RE.search(typ) or re.search(r"ESTATE OF|HEIRS", c["style"], re.I):
                seen.add(c["case_number"])
                other.append({**base, "style": f"{c['style']} [party: {person}]"})
    return {"cases": estates, "other": other}


def _style_decedent(style: str, surname: str = "") -> str:
    """Bare case title -> decedent in prose order. Travis writes both orders ('GREEN RUBY',
    'ALBERT L AMEZQUITA'): a surname shared with the searched party shows which end is the
    surname; otherwise a trailing initial means LAST FIRST M, 3+ words prose, 2 words LAST FIRST."""
    s = _ws(re.sub(r",?\s*(DECEDENT|DECEASED)\b.*$", "", style or "", flags=re.I))
    s = _ws(re.sub(r"\bA/?K/?A\b.*$", "", s, flags=re.I))
    if "," in s:
        return display_name(s)
    t = s.split()
    if len(t) < 2:
        return s.title() or "(decedent not shown)"
    sur = surname.upper()
    if sur and t[-1].upper() == sur and t[0].upper() != sur:
        last_first = False
    elif sur and t[0].upper() == sur:
        last_first = True
    else:
        last_first = len(t) == 2 or len(t[-1].strip(".")) == 1
    return " ".join(t[1:] + t[:1] if last_first else t).title()


def _estate_style(style: str) -> str:
    """'IN THE ESTATE OF Eleanor A. Smith, DECEDENT' -> 'Estate Of Eleanor A. Smith, Deceased'."""
    s = re.sub(r",?\s*(DECEDENT|DECEASED)\b.*$", ", Deceased", _ws(style), flags=re.I)
    return re.sub(r"^.*?\bESTATE OF\b", "Estate Of", s, flags=re.I)


# ── live search (headed Chromium, home IP) ───────────────────────────

class PortalSession:
    def __init__(self, county: str, browser):
        self.county = county
        self.base = PORTALS[county]
        self.host = re.match(r"https://[^/]+", self.base).group(0)
        self.ctx = browser.new_context(ignore_https_errors=True)
        self.page = self.ctx.new_page()
        self.solves = 0

    def _captcha(self) -> None:
        pg = self.page
        if pg.evaluate("() => (document.getElementById('Settings_CaptchaEnabled')||{}).value") != "True":
            return
        if self.solves >= MAX_SOLVES:
            raise RuntimeError(f"captcha budget reached ({MAX_SOLVES} solves)")
        import config  # noqa: PLC0415
        from twocaptcha import TwoCaptcha  # noqa: PLC0415
        sk = pg.get_attribute("[data-sitekey]", "data-sitekey")
        tok = TwoCaptcha(config.CAPTCHA_API_KEY).recaptcha(sitekey=sk, url=pg.url)["code"]
        self.solves += 1
        pg.evaluate("t => document.querySelectorAll('textarea[name=\"g-recaptcha-response\"]')"
                    ".forEach(x => x.value = t)", tok)

    def search(self, query: str) -> list[dict]:
        pg = self.page
        pg.goto(self.base + "Home/Dashboard/29", wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_selector("#caseCriteria_SearchCriteria", timeout=60000)
        pg.fill("#caseCriteria_SearchCriteria", query)
        self._captcha()
        pg.click("#btnSSSubmit")
        pg.wait_for_selector('a.nolink[data-party-id], p:has-text("No cases match"), '
                             'div:has-text("No cases match")', timeout=90000)
        if pg.locator("a.nolink[data-party-id]").count() == 0:
            return []
        # the party grid pages at 10; show them all (a common name can have 40+)
        total = pg.evaluate("() => { const m = (document.querySelector('.k-pager-info')||{}).innerText"
                            ".match(/of (\\d+) items/); return m ? +m[1] : 0 }")
        if total > 10:
            pg.evaluate("() => { const g = window.jQuery && jQuery('[data-role=grid]').first().data('kendoGrid');"
                        " if (g) g.dataSource.pageSize(200); }")
            pg.wait_for_timeout(4000)
        return parse_results(pg.content())

    def detail(self, url: str) -> dict:
        pg = self.page
        pg.goto(url if url.startswith("http") else self.host + url, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_selector("text=/Case Summary|Case Information/", timeout=60000)
        pg.wait_for_timeout(1500)
        return parse_detail(pg.inner_text("body"))


def search_names_parallel(county: str, names: list[str], cache: dict, save=None, workers: int = 3) -> dict:
    """search_names over ``workers`` browsers at once (each thread owns its own Playwright)."""
    import threading  # noqa: PLC0415
    todo = [n for n in names if n not in cache]
    if workers <= 1 or len(todo) < 2 * workers:
        return search_names(county, todo, cache, save)
    lock = threading.Lock()

    def merged_save(part: dict) -> None:
        with lock:
            cache.update(part)
            if save:
                save(cache)
    chunks = [todo[i::workers] for i in range(workers)]
    threads = [threading.Thread(target=search_names, args=(county, ch, {}, merged_save), daemon=True)
               for ch in chunks]
    for t in threads:
        t.start()
        time.sleep(5)
    for t in threads:
        t.join()
    return cache


def search_names(county: str, names: list[str], cache: dict, save=None, every: int = 10) -> dict:
    """Search each 'First [Middle] Last' name not already in ``cache``; returns the updated cache."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    todo = [n for n in names if n not in cache]
    if not todo:
        return cache
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        s = PortalSession(county, browser)
        for k, full in enumerate(todo, 1):
            first, last = _first_last(full)
            if not first:
                continue
            try:
                res = classify(s.search(f"{last.title()}, {first.title()}"), first, last)
                for c in (res["cases"] + res["other"])[:MAX_DETAILS]:
                    if c.get("url"):
                        try:
                            d = s.detail(c["url"])
                            c.update({k_: d.get(k_) for k_ in ("filed", "subtype", "letters_date", "inventory_date",
                                                               "heirship_date", "sale_date", "last_event_date",
                                                               "parties", "pr_name", "pr_role", "attorney_name")})
                            if d.get("status"):
                                c["type_status"] = f"{c['type_status'].split(' / ')[0]} / {d['status']}"
                            if d.get("style") and c in res["other"]:
                                c["style"] = f"{d['style']} [party: {c['party']}]"
                            c["events"] = d.get("events", [])[-12:]
                        except Exception as e:  # noqa: BLE001
                            c["detail_error"] = str(e)[:120]
                cache[full] = {**res, "searched": date.today().isoformat()}
                fails = 0
                logger.info("%s docket %-28s -> %d estate, %d other", county, full, len(res["cases"]),
                            len(res["other"]))
            except Exception as e:  # noqa: BLE001
                fails += 1
                logger.warning("%s docket %s failed: %s", county, full, str(e)[:160])
                if fails >= 3:
                    logger.error("%s docket: 3 failures in a row - stopping this county", county)
                    break
            if save and k % every == 0:
                save(cache)
            time.sleep(1.0)
        logger.info("%s docket: %d names searched, %d captcha solves", county, len(todo), s.solves)
        browser.close()
    if save:
        save(cache)
    return cache

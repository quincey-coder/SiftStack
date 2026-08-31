"""Austin Building & Standards Commission (BSC) - condemned-structure scraper.

The BSC is the body that legally "condemns" a structure in Austin (Tex. Loc.
Gov't Code 214.001 + City Code 25-12 Art. 9): it hears substandard-structure
cases and issues orders to REPAIR, VACATE, or DEMOLISH, with weekly civil
penalties. Its monthly agendas are text PDFs on EDIMS listing, per case:

    Case Number: CL 2026-083909
    Property address: 9204 Curlew Drive / Owners: Thomas A. Rodriguez & Casimiro Rodriguez
    Staff presenter: Brandon Smith
    Staff recommendation: Repair residential structure.

Returning cases reuse the original CL number and add
    Previous commission action: Order TRV 2025142639 issued December 9, 2025, for repair ...
(the TRV number is the recorded Travis clerk instrument).

Source index: https://www.austintexas.gov/boards-commissions/meetings/{YEAR}_17_{PAGE}
(board id 17, 8 rows/page, server-rendered, UNQUOTED html attributes, flat
sibling divs). PDFs: https://services.austintexas.gov/edims/document.cfm?id=N

Each CL case becomes NoticeData(notice_type="condemned", county="Travis").
Agendas carry NO city/ZIP/parcel, so each address is resolved through the
Austin Code complaint dataset (Socrata 6wtj-zbtb - every BSC property has a
code-case history) to a TCAD parcel + ZIP, then the parcel to the CAD owner via
travis_tax_cache. Hard-won facts are inline below; the full source recon is in
the plan (2026-08-29).

Supported agenda formats: D (2022-) and C (2018-21) fully; B (2016-17,
"Property Address/Owner: addr / owner") best-effort; A (<=2016 label-less
tables) and scanned 2010-12 are out of scope - they never occur inside the
12-month historical window.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text

from notice_parser import NoticeData, _detect_entity_type
from scrapers import register, ScraperError
from scrapers import bsc_state
from scrapers.debug_capture import dump_text

logger = logging.getLogger(__name__)

# Module globals collected by main.py (mirrors code_enforcement_travis).
LAST_RUN_DIFF: dict | None = None
LAST_RUN_REPORT_PATH = None
LAST_ALARMS: list = []

INDEX_URL = "https://www.austintexas.gov/boards-commissions/meetings/{year}_17_{page}"
EDIMS_URL = "https://services.austintexas.gov/edims/document.cfm?id={id}"
SOCRATA_URL = "https://data.austintexas.gov/resource/6wtj-zbtb.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_DELAY = 0.5      # Imperva returns 200+empty body on bursts; be polite
REQUEST_TIMEOUT = 45
MAX_RETRIES = 3
HISTORICAL_DAYS = 365
DAILY_LOOKBACK_DAYS = 90  # daily: re-read recent meetings; seen_docs skips parsed PDFs
ORDER_RANK = bsc_state.ORDER_RANK

_INDEX_SENTINEL = "bcic_nav"


@dataclass
class Meeting:
    date: str                       # YYYY-MM-DD
    cancelled: bool
    docs: list = field(default_factory=list)   # [(label, edims_id)]


@dataclass
class BscItem:
    case_ids: list                  # canonical "CL 2026-083909", sorted
    other_ids: list                 # CV/OL (appeal) numbers, informational
    section: str                    # new | returning | update | appeal | hearings | other
    address_raw: str
    owner_raw: str
    recommendation: str
    previous_action: str
    rec_class: str                  # demolish|vacate|repair|uphold_penalties|deny_appeal|other
    prior_order_class: str
    trv_number: str
    repeat_offender: bool
    multifamily: bool
    block_text: str


# =====================================================================
# Text normalization (pure)
# =====================================================================
_CASE_TOKEN_RE = re.compile(r"\b(CL|CV|OL)\s*(\d{4})\s*-\s*\n?\s*(\d{5,7})\b", re.I)
_PAGE_FOOTER_RE = re.compile(r"^\s*Page\s+\d+(\s+of\s+\d+)?\s*$", re.I | re.M)
_BARE_ITEM_NUM_RE = re.compile(r"^\s*\d{1,2}\.\s*$", re.M)

_CHAR_MAP = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ", "￾": "", "﻿": "",
    "\x0c": "\n",
})


def _normalize_text(text: str) -> str:
    """Make pdfminer output regex-safe: ASCII punctuation, no page footers /
    bare item numbers, wrapped case numbers rejoined, whitespace collapsed."""
    t = (text or "").translate(_CHAR_MAP)
    t = _PAGE_FOOTER_RE.sub("", t)
    t = _BARE_ITEM_NUM_RE.sub("", t)
    # "CL 2020-\n136461" / "CL 2024- 076044" -> "CL 2024-076044"
    t = _CASE_TOKEN_RE.sub(lambda m: f"{m.group(1).upper()} {m.group(2)}-{m.group(3)}", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


_SECTION_PATTERNS = [
    (re.compile(r"^\s*(New\s+Case\(?s?\)?|Case\(s\))\s*:?", re.I), "new"),
    (re.compile(r"^\s*(Returning\s+Case\(?s?\)?|RETURNING\s+CASES|Returning\s+for\s+Commission\s+Action)\s*:?", re.I), "returning"),
    (re.compile(r"^\s*(Update\s+Case\(?s?\)?|UPDATE\s+CASES)\s*:?", re.I), "update"),
    (re.compile(r"^\s*(Appeal\(?s?\)?|APPEALS?)\s*:?(\s|$)", re.I), "appeal"),
    (re.compile(r"^\s*PUBLIC\s+HEARINGS?\s*:?\s*$", re.I), "hearings"),
    (re.compile(r"^\s*(DISCUSSION|STAFF\s+BRIEFING|FUTURE\s+AGENDA|ADJOURN|EXECUTIVE\s+SESSION|APPROVAL\s+OF\s+MINUTES)", re.I), "other"),
]

_LABEL_SUBS = [
    # item number glued to the label: "2.  Case Number: CL ..."
    (re.compile(r"^\s*(?:\d{1,2}\.\s*)?Case\s+Number(?:s|\(s\))?\s*:?\s*", re.I | re.M), "Case Number: "),
    # format B first (it carries both values on one line)
    (re.compile(r"^\s*Property\s+Address\s*/\s*Owners?\s*:?\s*", re.I | re.M), "Property Address: "),
    (re.compile(r"^\s*/?\s*Property\s+Address\s*:?\s*/?\s*", re.I | re.M), "Property Address: "),
    (re.compile(r"\bOwners?\s*/\s*Appellants?\s*:", re.I), "Owner:"),
    (re.compile(r"\bOwners?\s*:", re.I), "Owner:"),
    (re.compile(r"\bAppellants?\s*:", re.I), "Appellant:"),
    (re.compile(r"^\s*Staff\s+(?:recommendation|purpose)\s*:?\s*", re.I | re.M), "Staff Recommendation: "),
    (re.compile(r"^\s*(?:Staff\s+)?Presenter\s*:?\s*", re.I | re.M), "Staff Presenter: "),
    (re.compile(r"^\s*Previous\s+Commission\s+action\s*:?\s*", re.I | re.M), "Previous Commission Action: "),
]


def _canonicalize_labels(text: str) -> str:
    """One spelling per field label + '## SECTION x' sentinel lines."""
    out_lines = []
    for line in text.split("\n"):
        replaced = False
        for pat, name in _SECTION_PATTERNS:
            if pat.match(line):
                out_lines.append(f"## SECTION {name}")
                replaced = True
                break
        if not replaced:
            out_lines.append(line)
    t = "\n".join(out_lines)
    for pat, repl in _LABEL_SUBS:
        t = pat.sub(repl, t)
    return t


def _split_sections(text: str) -> list:
    sections: list = [("other", [])]
    for line in text.split("\n"):
        m = re.match(r"^## SECTION (\w+)", line)
        if m:
            sections.append((m.group(1), []))
        else:
            sections[-1][1].append(line)
    return [(name, "\n".join(lines)) for name, lines in sections]


def _split_items(body: str) -> list:
    """Split a section body at each 'Case Number:' label (label-anchored -
    item numbers and page footers are unreliable in extracted text)."""
    idxs = [m.start() for m in re.finditer(r"Case Number: ", body)]
    if not idxs:
        return []
    idxs.append(len(body))
    return [body[a:b].strip() for a, b in zip(idxs, idxs[1:])]


def _extract_case_ids(text: str) -> tuple:
    """(CL ids, CV/OL ids) in canonical 'CL 2026-083909' form, sorted/unique.
    Handles the 'CL and 2019-144426' typo (bare number inherits the last
    prefix) and 'A thru B' ranges (expanded only when same year, span <= 5)."""
    cl, other = set(), set()
    last_prefix = "CL"
    for m in re.finditer(r"\b(CL|CV|OL)\s*(\d{4})-(\d{5,7})\b|(?<![\w-])(\d{4})-(\d{5,7})\b", text, re.I):
        if m.group(1):
            last_prefix = m.group(1).upper()
            year, num = m.group(2), m.group(3)
        else:
            year, num = m.group(4), m.group(5)
        cid = f"{last_prefix} {year}-{num}"
        (cl if last_prefix == "CL" else other).add(cid)
    # "2015-098835 thru 2015-098886"
    for m in re.finditer(r"(\d{4})-(\d{5,7})\s+thru\s+(\d{4})-(\d{5,7})", text, re.I):
        y1, n1, y2, n2 = m.groups()
        if y1 == y2 and 0 < int(n2) - int(n1) <= 5:
            for n in range(int(n1), int(n2) + 1):
                cl.add(f"CL {y1}-{str(n).zfill(len(n1))}")
    return sorted(cl), sorted(other)


def _classify_recommendation(text: str) -> str:
    t = (text or "").lower()
    if not t.strip():
        return "other"
    if re.search(r"demoli(sh|tion)", t):
        return "demolish"
    if "vacate" in t:
        return "vacate"
    if re.search(r"\brepair", t):
        return "repair"
    if re.search(r"uphold|accrued penalt", t):
        return "uphold_penalties"
    if re.search(r"deny.*appeal|appeal.*den", t):
        return "deny_appeal"
    return "other"


def _parse_previous_action(text: str) -> tuple:
    """(prior order class, 'TRV NNNNNNNNNN' or '') from a Previous Commission
    Action line."""
    t = text or ""
    trv = ""
    m = re.search(r"\bTRV\s*(\d{8,12})\b", t, re.I)
    if m:
        trv = f"TRV {m.group(1)}"
    low = t.lower()
    cls = ""
    m2 = re.search(r"\b(?:for|to)\s+(repair|demoli(?:sh|tion)|vacat\w*)", low)
    if m2:
        w = m2.group(1)
        cls = "repair" if w.startswith("repair") else "demolish" if w.startswith("demoli") else "vacate"
    elif re.search(r"demoli(sh|tion)", low):
        cls = "demolish"
    elif "vacate" in low:
        cls = "vacate"
    elif "repair" in low:
        cls = "repair"
    return cls, trv


# =====================================================================
# Address + owner cleaning (pure)
# =====================================================================
_MULTIFAMILY_RE = re.compile(
    r"\b(bldgs?|buildings?|units?|apartments?|apts?|exteriors?|interiors?|condos?)\b|#\s*\d", re.I)
_ADDR_DESCRIPTOR_CUT_RE = re.compile(
    r"\s*[,;/\-(]\s*(?=(?:and\s+)?(?:bldgs?|buildings?|units?|apts?|exteriors?|interiors?|fence|suite|office)\b)", re.I)
_PAREN_RE = re.compile(r"\s*\(([^)]*)\)")
_AKA_SPLIT_RE = re.compile(r"\s*(?:,\s*)?(?:\(?\s*a/?k/?a\.?\s+|\(?\s*also\s+known\s+as\s+)", re.I)
_ORDINAL_FIX_RE = re.compile(r"\b(\d+)(St|Nd|Rd|Th)\b")
_UPPER_TOKENS = {"IH", "FM", "RM", "RR", "US", "SH", "EB", "WB", "NB", "SB", "NE", "NW", "SE", "SW"}


def _title_case_addr(s: str) -> str:
    from scrapers.travis_texdel_cleaner import title_case
    out = title_case(s)
    out = _ORDINAL_FIX_RE.sub(lambda m: m.group(1) + m.group(2).lower(), out)
    return " ".join(w.upper() if w.upper() in _UPPER_TOKENS else w for w in out.split(" "))


def _clean_address(raw: str) -> tuple:
    """-> (street address, multifamily flag, note with the stripped detail).

    Keeps unit/building designators OUT of the address (enrichment's condo
    filter keys on them) and hands them back as a note for the Notes field."""
    s = re.sub(r"\s+", " ", (raw or "").replace("\n", " ")).strip(" /;:,-")
    if not s:
        return "", False, ""
    multifamily = bool(_MULTIFAMILY_RE.search(s))
    note_parts: list = []
    parts = _AKA_SPLIT_RE.split(s, maxsplit=1)
    if len(parts) > 1:
        s, aka = parts[0].strip(" ,;/-"), parts[1].strip(" ()")
        note_parts.append(f"aka {aka}")
    m = _ADDR_DESCRIPTOR_CUT_RE.search(s)
    if m:
        note_parts.append(s[m.end():].strip(" ,;/-()"))
        s = s[:m.start()]
    # Any remaining parenthetical ("(units 112-314)", "(the Social)") is detail, not address.
    for pm in _PAREN_RE.finditer(s):
        if pm.group(1).strip():
            note_parts.append(pm.group(1).strip())
    s = _PAREN_RE.sub("", s)
    # "2015 and 2017 Payne Ave" / "1203-1205 E 6th" -> first house number
    s = re.sub(r"^(\d+[A-Za-z]?)\s*(?:and|&|-|thru)\s*\d+[A-Za-z]?\s+", r"\1 ", s, flags=re.I)
    s = re.sub(r",?\s*Austin(?:,\s*T(?:X|exas))?(?:\s*\d{5})?\s*$", "", s, flags=re.I)
    s = s.replace(".", "").strip(" ,;/-")
    s = re.sub(r"\s+", " ", s)
    return _title_case_addr(s), multifamily, "; ".join(p for p in note_parts if p)


def _split_house_street(address: str) -> tuple:
    m = re.match(r"^\s*(\d+[A-Za-z]?)\s+(.+)$", address or "")
    if not m:
        return "", (address or "").strip()
    return m.group(1), m.group(2).strip()


_CO_SPLIT_RE = re.compile(r"\s*\bc\s*/\s*o\b\.?\s*", re.I)
_COOWNER_SPLIT_RE = re.compile(r"\s+and\s+|\s*&\s*", re.I)
_LIFE_ESTATE_RE = re.compile(r"\s*,?\s*\bLIFE\s+EST(?:ATE)?\b\.?", re.I)
_RELIGIOUS_RE = re.compile(
    r"\b(church|ministr(?:y|ies)|temple|congregation|synagogue|mosque|parish|diocese|chapel|iglesia)\b", re.I)


def _casefix(name: str) -> str:
    """Title-case only ALL-CAPS strings; agenda names are already mixed case."""
    from scrapers.travis_texdel_cleaner import title_case
    n = re.sub(r"\s+", " ", name or "").strip(" ,;/")
    return title_case(n) if n and n == n.upper() else n


def _parse_owner(raw: str) -> tuple:
    """-> (owner_name, entity_type, c_o_name, co_owner_names).

    entity_type: "government" (drop the record - the City can't sell to us),
    "business" (LLC/trust/church - the entity string IS the owner_name), or ""
    (person). Names are classified on the PRISTINE string before any stripping
    (see CLAUDE.md: entity names containing '&' get decapitated otherwise)."""
    from scrapers.travis_texdel_cleaner import is_business, strip_etux_etal
    s = re.sub(r"\s+", " ", (raw or "").replace("\n", " ")).strip(" ,;/")
    # "Bobbie Jean Leare (1704 Miriam Avenue LLC is grantee on TRV ...)" — the
    # parenthetical is commentary, never part of the name (it lands in raw_text).
    s = _PAREN_RE.sub("", s).strip(" ,;/")
    if not s:
        return "", "", "", []
    c_o = ""
    parts = _CO_SPLIT_RE.split(s, maxsplit=1)
    if len(parts) == 2:
        s, c_o = parts[0].strip(" ,;/"), parts[1].strip(" ,;/")
    # "Evelyn McKinley Life Estate" is a PERSON (a life tenant), not an entity;
    # the ESTATE token would otherwise classify it as a business.
    s = _LIFE_ESTATE_RE.sub("", s).strip(" ,;/")
    etype = _detect_entity_type(s)
    if etype == "government" and _RELIGIOUS_RE.search(s):
        # Churches/ministries trip the government keyword set but are private
        # nonprofits that CAN sell — treat as a business entity, never drop.
        etype = "business"
    if not etype and is_business(s):
        etype = "business"
    if etype:
        return _casefix(s), etype, _casefix(c_o), []
    s = strip_etux_etal(s, strip_spousal_tail=False)
    names = [p.strip(" ,") for p in _COOWNER_SPLIT_RE.split(s) if p.strip(" ,")]
    names = [re.sub(r"^(?:husband|wife|spouse)\s+", "", n, flags=re.I) for n in names]
    if not names:
        return "", "", _casefix(c_o), []
    return _casefix(names[0]), "", _casefix(c_o), [_casefix(n) for n in names[1:]]


def _reconcile_owner(agenda_owner: str, entity_type: str, cad_rec) -> tuple:
    """-> (tax_owner_name, flags). The agenda owner always stays owner_name (it
    is the party the City is serving); the CAD NAMELF goes to tax_owner_name
    and the formatter surfaces it in Notes when it differs."""
    from scrapers.travis_texdel_cleaner import is_business
    from obituary_enricher import _is_same_person
    flags: list = []
    fullname = ((cad_rec or {}).get("fullname") or "").strip()
    if not fullname:
        return "", ["cad_no_match"]
    cad_is_entity = bool(_detect_entity_type(fullname)) or is_business(fullname)
    if entity_type or cad_is_entity:
        if bool(entity_type) != cad_is_entity:
            flags.append("owner_entity_mismatch")
    elif agenda_owner and not _is_same_person(agenda_owner, fullname):
        flags.append("owner_mismatch_cad")
    return fullname, flags


# =====================================================================
# Item parser (pure)
# =====================================================================
_FIELD_LABELS = ("Property Address:", "Owner:", "Appellant:", "Staff Presenter:",
                 "Staff Recommendation:", "Previous Commission Action:")
_FIELD_LABEL_RE = re.compile("|".join(re.escape(lbl) for lbl in _FIELD_LABELS))
_ADDR_LINE_RE = re.compile(r"^\s*\d+[A-Za-z]?\s+(?:[NSEW]\.?\s+)?[A-Za-z]", re.M)


def _segments(text: str) -> dict:
    """{label: value} for each canonical label found (first occurrence wins)."""
    hits = list(_FIELD_LABEL_RE.finditer(text))
    segs: dict = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        label = m.group(0)
        val = text[m.end():end].replace("\n", " ")
        val = re.sub(r"\s+", " ", val).strip(" /")
        segs.setdefault(label, val)
    return segs


def _parse_item(block: str, section: str):
    """One 'Case Number: ...' block -> BscItem, or None for appeals / junk."""
    body = block[len("Case Number: "):] if block.startswith("Case Number: ") else block
    first_label = _FIELD_LABEL_RE.search(body)
    header_end = first_label.start() if first_label else len(body)
    header = body[:header_end]
    address_raw = ""
    # 2021-style item with NO 'Property address' label: the address line sits
    # right after the case numbers.
    am = _ADDR_LINE_RE.search(header)
    if am and not _CASE_TOKEN_RE.match(header[am.start():]):
        address_raw = header[am.start():]
        header = header[:am.start()]
    cl_ids, other_ids = _extract_case_ids(header)
    segs = _segments(body[header_end:]) if first_label else {}
    addr_val = segs.get("Property Address:", "")
    owner_val = segs.get("Owner:", "")
    if addr_val and not owner_val and " / " in addr_val:
        # format B: "addr / owner" on one line
        addr_val, owner_val = addr_val.rsplit(" / ", 1)
    if addr_val:
        address_raw = addr_val
    elif address_raw:
        address_raw = re.sub(r"\s+", " ", address_raw.replace("\n", " ")).strip(" /")
    recommendation = segs.get("Staff Recommendation:", "")
    previous = segs.get("Previous Commission Action:", "")
    if not cl_ids:
        return None
    rec_class = _classify_recommendation(recommendation)
    if section == "appeal" or rec_class == "deny_appeal":
        return None
    prior_cls, trv = _parse_previous_action(previous)
    if not trv:
        _, trv = _parse_previous_action(recommendation)
    joined = f"{address_raw} {recommendation} {previous}"
    return BscItem(
        case_ids=cl_ids, other_ids=other_ids, section=section,
        address_raw=address_raw, owner_raw=owner_val,
        recommendation=recommendation, previous_action=previous,
        rec_class=rec_class, prior_order_class=prior_cls, trv_number=trv,
        repeat_offender=bool(re.search(r"repeat offender|\bROP\b", joined, re.I)),
        multifamily=bool(_MULTIFAMILY_RE.search(address_raw)) or bool(re.search(r"multi-?family", joined, re.I)),
        block_text=re.sub(r"\s+", " ", block).strip(),
    )


def parse_agenda_text(text: str) -> tuple:
    """Full agenda text -> (items, appeals_skipped). Pure; used by tests."""
    canon = _canonicalize_labels(_normalize_text(text))
    items: list = []
    skipped = 0
    for section, body in _split_sections(canon):
        if section == "other" and "Case Number: " not in body:
            continue
        for block in _split_items(body):
            item = _parse_item(block, section)
            if item is None:
                skipped += 1
            else:
                items.append(item)
    return items, skipped


# =====================================================================
# Index + PDF fetching
# =====================================================================
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
    return s


def _get(s: requests.Session, url: str, *, sentinel=None, want_pdf: bool = False) -> requests.Response:
    """GET with retry/backoff. Success = 200 AND (sentinel in body | %PDF magic)."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = s.get(url, timeout=REQUEST_TIMEOUT)
            body_ok = resp.status_code == 200 and (
                (want_pdf and resp.content[:5] == b"%PDF-") or
                (not want_pdf and (sentinel is None or sentinel in resp.text))
            )
            if body_ok:
                time.sleep(REQUEST_DELAY)
                return resp
            last_err = ValueError(
                f"HTTP {resp.status_code}, {len(resp.content)} bytes, "
                f"{'no %PDF magic' if want_pdf else 'sentinel missing'}"
            )
        except requests.RequestException as e:
            last_err = e
        logger.warning("BSC fetch failed (%s) attempt %d/%d: %s", url, attempt, MAX_RETRIES, last_err)
        time.sleep(2 ** (attempt - 1))
    raise requests.RequestException(f"BSC fetch failed after {MAX_RETRIES} attempts: {url}: {last_err}")


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def _parse_meeting_date(text: str) -> tuple:
    """'August 26, 2026 (Cancelled)' -> ('2026-08-26', True)."""
    cancelled = "cancel" in text.lower()
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})", text)
    if not m or m.group(1).lower() not in _MONTHS:
        return "", cancelled
    return f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}", cancelled


def _parse_index_page(html: str) -> tuple:
    """Flat sibling divs: bcic_mtgdate -> [bcic_mtgtype] -> bcic_doc*.
    Returns (meetings, page, total_pages)."""
    soup = BeautifulSoup(html, "html.parser")
    meetings: list = []
    for div in soup.find_all("div", class_=["bcic_mtgdate", "bcic_doc"]):
        cls = div.get("class") or []
        if "bcic_mtgdate" in cls:
            date, cancelled = _parse_meeting_date(div.get_text(" ", strip=True))
            meetings.append(Meeting(date=date, cancelled=cancelled))
        elif meetings:
            label = div.find("b")
            label_txt = label.get_text(strip=True) if label else div.get_text(" ", strip=True)
            a = div.find("a", href=True)
            href = a["href"] if a else ""
            m = re.search(r"document\.cfm\?id=(\d+)", href, re.I)
            if m:
                pair = (label_txt, int(m.group(1)))
                if pair not in meetings[-1].docs:
                    meetings[-1].docs.append(pair)
    page, total = 1, 1
    nav = soup.find("div", class_="bcic_nav")
    if nav:
        m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", nav.get_text(" ", strip=True), re.I)
        if m:
            page, total = int(m.group(1)), int(m.group(2))
    return [mt for mt in meetings if mt.date], page, total


def _list_meetings(s: requests.Session, years: list, min_date: str) -> list:
    out: list = []
    for year in years:
        page = 1
        while True:
            url = INDEX_URL.format(year=year, page=page)
            html = _get(s, url, sentinel=_INDEX_SENTINEL).text
            meetings, cur, total = _parse_index_page(html)
            if not meetings and page == 1:
                LAST_ALARMS.append(f"BSC index page {url} parsed 0 meetings - markup drift?")
                dump_text(f"bsc_index_zero_{year}_{page}", html)
            out.extend(meetings)
            oldest = min((m.date for m in meetings), default="")
            if page >= total or (oldest and oldest < min_date):
                break
            page += 1
    seen: set = set()
    uniq: list = []
    for m in sorted(out, key=lambda mt: mt.date):
        key = (m.date, tuple(m.docs))
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    return uniq


def _pick_agenda_doc(m: Meeting):
    if m.cancelled:
        return None
    revised = [i for lbl, i in m.docs if lbl.lower().startswith("revised agenda")]
    plain = [i for lbl, i in m.docs if lbl.lower().startswith("agenda")]
    if revised:
        return max(revised)
    return max(plain) if plain else None


def _fetch_pdf(s: requests.Session, edims_id: int) -> tuple:
    resp = _get(s, EDIMS_URL.format(id=edims_id), want_pdf=True)
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename="?([^";]+)', cd)
    return resp.content, (m.group(1) if m else "")


def _pdf_text(pdf_bytes: bytes) -> str:
    # pdfminer keeps the space at line-wrap boundaries; pypdfium2 drops it
    # ("Royal CrestDrive") - never swap this for the OCR path's extractor.
    return extract_text(io.BytesIO(pdf_bytes)) or ""


# =====================================================================
# Resolution: address -> parcel / ZIP / owner
# =====================================================================
def _norm_street(s: str) -> str:
    from travis_tax_cache import _normalize_street
    return _normalize_street(s)


_DIRECTIONALS = ("N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST")


def _street_stems(street: str) -> list:
    """Ordered LIKE-prefix candidates for the Socrata street_name column.

    Austin stores the directional INSIDE street_name ("S 1ST ST", "E 12TH ST"),
    so a directional address must be probed with the directional first;
    the bare stem is the fallback (and the only probe when there is none)."""
    toks = [t for t in re.sub(r"[.,]", "", _norm_street(street)).split() if t]
    if not toks:
        return []
    if toks[0] in _DIRECTIONALS and len(toks) > 1:
        return [f"{toks[0]} {toks[1]}", toks[1]]
    return [toks[0]]


def _socrata_lookup(s: requests.Session, house: str, street: str, memo: dict) -> dict:
    """Austin Code complaint dataset as an address gazetteer ->
    {"parcelid", "zip_code", "flag"}. Every BSC property has code-case
    history there (8/8 live), and its parcelid is the 10-digit TCAD geo_id."""
    stems = _street_stems(street)
    if not house or not stems:
        return {}
    key = (house, stems[0])
    if key in memo:
        return memo[key]
    headers = {"Accept": "application/json"}
    token = os.getenv("SOCRATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    rows: list = []
    for stem in stems:
        stem_q = stem.replace("'", "''")
        params = {
            "$select": "parcelid,zip_code,street_name,count(*) as n",
            "$where": f"house_number='{house}' AND upper(street_name) like '{stem_q}%'",
            "$group": "parcelid,zip_code,street_name",
            "$order": "n DESC",
            "$limit": "20",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = s.get(SOCRATA_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                rows = resp.json()
                break
            except (requests.RequestException, ValueError) as e:
                logger.warning("BSC Socrata lookup failed (%s %s) attempt %d: %s", house, stem, attempt, e)
                time.sleep(2 * attempt)
        if rows:
            break
    result: dict = {}
    target = _norm_street(f"{house} {street}")
    exact = [r for r in rows if _norm_street(f"{house} {r.get('street_name', '')}") == target]
    cands = exact or rows
    parcels = {(r.get("parcelid") or "").strip() for r in cands if r.get("parcelid")}
    if exact or len(parcels) == 1:
        best = sorted(cands, key=lambda r: -int(r.get("n") or 0))[0]
        zip5 = str(best.get("zip_code") or "").split(".")[0][:5]
        result = {
            "parcelid": (best.get("parcelid") or "").strip(),
            "zip_code": zip5 if zip5.isdigit() else "",
            "flag": "" if exact else "parcel_stem_match",
        }
    memo[key] = result
    return result


def _cad_lookup(parcelid: str, street: str):
    try:
        from travis_tax_cache import search_by_parcel, search_by_address
    except ImportError:
        return None
    try:
        rec = search_by_parcel(parcelid) if parcelid else None
        if not rec and street:
            rec = search_by_address(street, "")
        return rec
    except Exception as e:  # cache download/parse trouble must not kill the scrape
        logger.warning("BSC CAD lookup failed for %s / %s: %s", parcelid, street, e)
        return None


# =====================================================================
# Notice builder
# =====================================================================
def _fmt_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return iso


def _first_sentence(text: str, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.+?[.;])(\s|$)", t)
    t = m.group(1) if m else t
    return (t[: limit - 1] + "...") if len(t) > limit else t


def _build_notice(item: BscItem, meeting_date: str, edims_id: int, *,
                  address: str, addr_note: str, zip5: str, parcel: str,
                  owner_name: str, entity_type: str, c_o_name: str, co_owners: list,
                  cad_rec, tax_owner_name: str, flags: list,
                  effective_cls: str, is_returning: bool, escalated_from: str) -> NoticeData:
    from scrapers.travis_texdel_cleaner import city_for_zip, title_case
    from travis_tax_cache import _normalize_parcel

    case_id = item.case_ids[0]
    when = _fmt_date(meeting_date)
    kind = "Returning" if is_returning else "New"
    desc_bits = [
        f"Condemned structure - BSC hearing {when} ({kind} case {case_id}): "
        f"staff recommends {_first_sentence(item.recommendation) or item.rec_class or 'see agenda'}"
    ]
    if item.previous_action:
        desc_bits.append(f"prior action: {_first_sentence(item.previous_action)}")
    if item.repeat_offender:
        desc_bits.append("repeat offender program")
    if addr_note:
        desc_bits.append(addr_note)
    if len(item.case_ids) > 1:
        desc_bits.append("also " + ", ".join(item.case_ids[1:]))
    if co_owners:
        desc_bits.append("co-owner(s): " + ", ".join(co_owners))

    if escalated_from:
        status = f"Open - returning, escalated {escalated_from}->{effective_cls}"
    elif is_returning:
        status = f"Open - returning (staff rec: {item.rec_class}; order: {effective_cls})"
    else:
        status = f"Open - new case (staff rec: {item.rec_class})"

    deadline = ""
    m = re.search(r"within\s+(\d{1,3})\s+days", item.recommendation, re.I)
    if m:
        try:
            deadline = (datetime.strptime(meeting_date, "%Y-%m-%d") + timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
        except ValueError:
            deadline = ""

    n = NoticeData(
        date_added=meeting_date,
        address=address,
        city=city_for_zip(zip5) if zip5 else "Austin",
        state="TX",
        zip=zip5,
        owner_name=owner_name,
        notice_type="condemned",
        county="Travis",
        source_url=f"{EDIMS_URL.format(id=edims_id)}#case={case_id.replace(' ', '-')}",
        raw_text=" | ".join(b for b in (
            f"BSC {meeting_date}", item.section, "Case(s): " + ", ".join(item.case_ids),
            f"Address: {item.address_raw}", f"Owner: {item.owner_raw}",
            f"Rec: {item.recommendation}", f"Prev: {item.previous_action}" if item.previous_action else "",
            f"Agenda EDIMS {edims_id}",
        ) if b),
        parcel_id=_normalize_parcel(parcel) if parcel else "",
        violation_description="; ".join(desc_bits),
        severity="structure_condition",
        compliance_deadline=deadline,
        case_status=status,
        case_id=case_id,
        tax_owner_name=tax_owner_name,
    )
    n.order_class = effective_cls
    if flags:
        n.missing_data_flags = "|".join(flags)
    if cad_rec:
        # Owner MAILING address from the cache's mailing keys (both rolls carry
        # them since 2026-08-29; the legacy fallback reads the pre-fix shape
        # where current-roll records stored the mailing in "situsaddress").
        mail = cad_rec.get("mailing") or (
            cad_rec.get("situsaddress") if cad_rec.get("source") == "current_mailing" else ""
        )
        if mail:
            n.owner_street = title_case(mail)
            n.owner_city = title_case(cad_rec.get("mcity") or cad_rec.get("scity") or "")
            n.owner_state = cad_rec.get("mstate") or cad_rec.get("sstate") or "TX"
            n.owner_zip = (cad_rec.get("mzip") or cad_rec.get("szip") or "")[:5]
        if cad_rec.get("source") == "delinquent_situs":
            if cad_rec.get("totalpropmktvalue"):
                n.assessed_value = str(cad_rec["totalpropmktvalue"])
                n.assessed_source = "travis_cad"
            if cad_rec.get("delinquent_total"):
                n.tax_delinquent_amount = str(cad_rec["delinquent_total"])
            if cad_rec.get("years_delinquent"):
                n.tax_delinquent_years = str(cad_rec["years_delinquent"])
    if c_o_name:
        n.decision_maker_name = c_o_name
        n.decision_maker_source = "bsc_agenda_c_o"
        n.decision_maker_status = "tax_caretaker"
        n.dm_relationship = "care_of_caretaker"
    if entity_type == "business":
        n.entity_type = "business"
    return n


# =====================================================================
# Scraper
# =====================================================================
@register("Travis", "condemned")
class AustinBscScraper:
    """Austin BSC agendas -> condemned-structure NoticeData."""

    def __init__(self):
        self.last_meta: dict = {}

    def _window(self, mode: str, since_date) -> tuple:
        today = datetime.now()
        if mode == "historical":
            days = HISTORICAL_DAYS
        else:
            days = DAILY_LOOKBACK_DAYS
            if since_date:
                try:
                    delta = (today - datetime.strptime(since_date, "%Y-%m-%d")).days
                    days = max(days, min(delta + 30, HISTORICAL_DAYS))
                except ValueError:
                    pass
        return (today - timedelta(days=days)).strftime("%Y-%m-%d"), days

    async def scrape(self, mode: str = "daily", since_date=None, max_notices=None) -> list:
        global LAST_RUN_DIFF, LAST_RUN_REPORT_PATH, LAST_ALARMS
        LAST_RUN_DIFF = None
        LAST_RUN_REPORT_PATH = None
        LAST_ALARMS = []
        self.last_meta = {
            "returned": 0, "kept": 0, "window_days": 0, "meetings": 0,
            "agendas_new": 0, "agendas_skipped_seen": 0, "appeals_skipped": 0,
            "zip_unresolved": 0, "owner_mismatch": 0, "parse_zero_docs": 0,
        }
        min_date, days = self._window(mode, since_date)
        self.last_meta["window_days"] = days
        today = datetime.now()
        years = list(range(int(min_date[:4]), today.year + 1))

        state = bsc_state.load_state()
        seen_docs: dict = state.get("seen_docs") or {}
        s = _session()
        meetings = [m for m in _list_meetings(s, years, min_date) if m.date >= min_date]
        self.last_meta["meetings"] = len(meetings)
        if not meetings and not LAST_ALARMS:
            logger.info("Austin BSC: no meetings on the index since %s", min_date)

        # -- Fetch + parse each new agenda --
        parsed: list = []   # (meeting_date, edims_id, items)
        seen_delta: dict = {}
        diff = bsc_state.DiffResult()
        for m in meetings:
            doc = _pick_agenda_doc(m)
            if doc is None:
                continue
            if mode != "historical" and str(doc) in seen_docs:
                self.last_meta["agendas_skipped_seen"] += 1
                continue
            try:
                pdf, cd_name = _fetch_pdf(s, doc)
                text = _pdf_text(pdf)
            except Exception as e:
                logger.warning("Austin BSC: agenda %s (%s) fetch/extract failed: %s", doc, m.date, e)
                continue
            if cd_name and "agenda" not in cd_name.lower():
                logger.warning("Austin BSC: EDIMS %s filename %r is not an agenda", doc, cd_name)
            dump_text(f"bsc_agenda_{doc}", text)
            items, skipped = parse_agenda_text(text)
            self.last_meta["appeals_skipped"] += skipped
            if not items and re.search(r"Case\s+Number", text, re.I) and skipped == 0:
                self.last_meta["parse_zero_docs"] += 1
                diff.parse_zero_docs += 1
                dump_text(f"bsc_parse_zero_{doc}", text)
                logger.error("Austin BSC: agenda %s (%s) has case text but parsed 0 items", doc, m.date)
                continue
            parsed.append((m.date, doc, items))
            seen_delta[str(doc)] = {"meeting": m.date, "kind": "agenda",
                                    "parsed": len(items), "seen": today.strftime("%Y-%m-%d")}
            diff.meetings.append(m.date)
            self.last_meta["agendas_new"] += 1
            logger.info("Austin BSC: agenda %s (%s): %d case item(s), %d appeal/other skipped",
                        doc, m.date, len(items), skipped)

        # -- Apply state (escalation) + resolve + build, oldest meeting first --
        notices_by_key: dict = {}
        memo: dict = {}
        returned = 0
        for meeting_date, doc, items in sorted(parsed, key=lambda t: t[0]):
            for item in items:
                returned += 1
                case_id = item.case_ids[0]
                address, multifamily, addr_note = _clean_address(item.address_raw)
                owner_name, entity_type, c_o_name, co_owners = _parse_owner(item.owner_raw)
                if entity_type == "government":
                    logger.info("Austin BSC: skipping government-owned %s (%s)", address, owner_name)
                    continue
                house, street = _split_house_street(address)
                res = _socrata_lookup(s, house, street, memo) if house else {}
                parcel = res.get("parcelid", "")
                zip5 = res.get("zip_code", "")
                cad_rec = _cad_lookup(parcel, address) if (parcel or house) else None
                if not zip5 and cad_rec:
                    src = cad_rec.get("source")
                    # delinquent_situs = property ZIP; an address-index hit on a
                    # current_mailing record means owner-occupied (mailing == situs).
                    if src == "delinquent_situs" or (not parcel and src == "current_mailing"):
                        zip5 = (cad_rec.get("szip") or "")[:5]
                if not parcel and cad_rec and cad_rec.get("quickrefid"):
                    parcel = cad_rec["quickrefid"]
                tax_owner_name, flags = _reconcile_owner(owner_name, entity_type, cad_rec)
                if res.get("flag"):
                    flags.append(res["flag"])
                if not zip5:
                    flags.append("zip_unresolved")
                    self.last_meta["zip_unresolved"] += 1
                if "owner_mismatch_cad" in flags:
                    self.last_meta["owner_mismatch"] += 1
                if multifamily and "multifamily" not in flags:
                    flags.append("multifamily")

                is_new, prior_cls, eff = bsc_state.update_case(
                    state, case_id, meeting_date,
                    bsc_state.effective_class(item.rec_class, item.prior_order_class),
                    trv=item.trv_number, parcel=parcel, address=address, zip5=zip5,
                    owner_name=owner_name,
                )
                if not eff:
                    eff = "repair"
                    flags.append("bsc_class_inferred")
                escalated_from = prior_cls if (prior_cls and eff != prior_cls) else ""
                is_returning = (not is_new) or item.section == "returning" or bool(item.previous_action)
                if is_new:
                    diff.new.append(case_id)
                else:
                    diff.returning.append(case_id)
                if escalated_from:
                    diff.escalated.append([case_id, escalated_from, eff])

                n = _build_notice(
                    item, meeting_date, doc, address=address, addr_note=addr_note,
                    zip5=zip5, parcel=parcel, owner_name=owner_name, entity_type=entity_type,
                    c_o_name=c_o_name, co_owners=co_owners, cad_rec=cad_rec,
                    tax_owner_name=tax_owner_name, flags=flags, effective_cls=eff,
                    is_returning=is_returning, escalated_from=escalated_from,
                )
                # One record per (case, effective class); the latest meeting wins.
                notices_by_key[(case_id, eff)] = n
                if max_notices and len(notices_by_key) >= max_notices:
                    break
            if max_notices and len(notices_by_key) >= max_notices:
                break

        notices = list(notices_by_key.values())
        self.last_meta["returned"] = returned
        self.last_meta["kept"] = len(notices)
        diff.skipped_appeals = self.last_meta["appeals_skipped"]

        if not max_notices:
            try:
                bsc_state.commit_run(state, diff, seen_delta)
                LAST_RUN_REPORT_PATH = bsc_state.write_report_json(diff, stats=dict(self.last_meta))
            except Exception as e:  # bookkeeping must never break the scrape
                logger.warning("Austin BSC: state commit failed: %s", e)
        LAST_RUN_DIFF = diff.to_dict()

        logger.info(
            "Austin BSC: %d case item(s) -> %d notice(s) (agendas new=%d, skipped seen=%d, "
            "appeals=%d, zip unresolved=%d, mode=%s, window=%dd)",
            returned, len(notices), self.last_meta["agendas_new"],
            self.last_meta["agendas_skipped_seen"], self.last_meta["appeals_skipped"],
            self.last_meta["zip_unresolved"], mode, days,
        )
        if diff.parse_zero_docs and not notices:
            raise ScraperError(
                f"Austin BSC: {diff.parse_zero_docs} agenda(s) had case text but parsed 0 items "
                "- agenda layout changed?", partial=notices,
            )
        return notices

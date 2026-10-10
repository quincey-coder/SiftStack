"""Williamson County foreclosure scraper — trustee sales index PDF + notice PDFs.

Source: https://apps.wilco.org/countyclerk/trustee_sales/
  {base}/{Month}/files.aspx                 — listing for a SALE month
  {base}/{Month}/{MM-DD-YYYY}_File_IDX.pdf  — typed index of every posted notice
  {base}/{Month}/{MM-DD-YYYY}_File_NNN.pdf  — one scanned notice each

Index columns: ENTRY NO | OWNER'S LAST NAME | FILE NO | CREATION DATE |
ADDRESS | CITY | LEGAL DESCRIPTION. Rows are sorted by owner; ENTRY NO is the
filing sequence (a permutation of 1..N).

Parsing (rewritten 2026-09-09): the clerk's layout changed (ENTRY NO column
first, cells merged across columns, month title interleaved) and the old
"zip the text blocks by position" parser returned 7 garbage rows out of 75.
The index is now read with pdfminer's LAYOUT API — the header labels give the
column x-bands, every character is bucketed by its x/y into a cell, and rows
are keyed on the integer ENTRY NO. Merged cells ("61 WASHGUYS 5 LLC",
"7/7/2026 297 KOONTS LOOP") split on their leading token.

Months with no index yet (October 2026: 85 image-scan notice PDFs) used to be
discarded. They are now read notice-by-notice under an OCR budget
(SIFT_WILCO_OCR_BUDGET_S, default 600 s) with the same per-PDF memory the Bell
scraper uses (scrapers.pdf_notice_state), so daily runs chip through the
backlog and replay finished PDFs for free.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta

import requests

from notice_parser import NoticeData
from scrapers import register
from scrapers import pdf_notice_state as pdf_state
from scrapers.pdf_ocr import budget_from_env, has_text, ocr_pdf_bytes, text_layer
from scrapers.trustee_notice_parse import parse_trustee_notice_text

logger = logging.getLogger(__name__)

BASE_URL = "https://apps.wilco.org/countyclerk/trustee_sales"
COUNTY = "Williamson"

# Bump when either parser changes so remembered notice PDFs are re-parsed.
PARSER_VERSION = 1
OCR_BUDGET_ENV = "SIFT_WILCO_OCR_BUDGET_S"
OCR_BUDGET_DEFAULT_S = 600.0

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

NOTICE_FILE_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})_File_(\d{3})\.pdf$", re.IGNORECASE)

_KNOWN_CITIES = (
    "ROUND ROCK", "CEDAR PARK", "GEORGETOWN", "LEANDER", "AUSTIN", "TAYLOR",
    "HUTTO", "LIBERTY HILL", "JARRELL", "FLORENCE", "PFLUGERVILLE", "MANOR",
    "GRANGER", "BARTLETT", "THRALL", "WEIR", "COUPLAND", "SALADO", "KILLEEN",
    "BRUSHY CREEK", "WALBURG", "SCHWERTNER",
)


# ── HTTP helpers ──────────────────────────────────────────────────────
def _get_month_file_list(month_name: str) -> list[str] | None:
    """Fetch the file listing page for a month and return PDF filenames.

    Returns None on an HTTP failure (so the caller can tell "fetch broke"
    apart from "month legitimately has no files yet").
    """
    url = f"{BASE_URL}/{month_name}/files.aspx"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return re.findall(r'HREF="([^"]+\.pdf)"', resp.text, re.IGNORECASE)
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s file listing: %s", month_name, e)
        return None


def _find_index_pdf(filenames: list[str]) -> str | None:
    """Find the index PDF filename (contains _IDX or _Index)."""
    for fn in filenames:
        if "IDX" in fn.upper() or "INDEX" in fn.upper():
            return fn
    return None


def _pdf_url(month_name: str, filename: str) -> str:
    return f"{BASE_URL}/{month_name}/{filename.split('/')[-1]}"


def _download_pdf_bytes(month_name: str, filename: str) -> bytes | None:
    url = _pdf_url(month_name, filename)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning("Failed to download %s: %s", url, e)
        return None


def _head_size(month_name: str, filename: str) -> int | None:
    """Content-Length from a HEAD request, None when unavailable."""
    try:
        resp = requests.head(_pdf_url(month_name, filename), timeout=10, allow_redirects=True)
        cl = resp.headers.get("Content-Length")
        return int(cl) if cl and cl.isdigit() else None
    except Exception:
        return None


def _download_pdf_text(month_name: str, filename: str) -> str:
    """Download a PDF and extract its text layer ('' when scanned/failed)."""
    data = _download_pdf_bytes(month_name, filename)
    return text_layer(data) if data else ""


def _first_tuesday(month_name: str, year: int) -> str:
    """Calculate the first Tuesday of a given month (YYYY-MM-DD)."""
    month_num = MONTHS.index(month_name) + 1
    d = datetime(year, month_num, 1)
    return (d + timedelta(days=(1 - d.weekday()) % 7)).strftime("%Y-%m-%d")


# ── Index PDF: layout-based parser ────────────────────────────────────
COLUMNS = ("entry", "owner", "file_no", "created", "address", "city", "legal")

# header label (upper, stripped, prefix match) -> column
_HEADER_TOKENS = (
    ("ENTRY", "entry"),
    ("OWNER", "owner"),
    ("FILE", "file_no"),
    ("CREATIO", "created"),   # "CREATIO" + "N DATE" wraps across two lines
    ("ADDRESS", "address"),
    ("CITY", "city"),
    ("LEGAL", "legal"),
)

# Column x0 measured on the 2026-09 index (page width 1721.7) — used only when
# a document's first page carries no header line.
_DEFAULT_X0 = {
    "entry": 114.8, "owner": 162.3, "file_no": 384.3, "created": 422.3,
    "address": 483.9, "city": 797.5, "legal": 895.6,
}
_BAND_PAD = 12.0       # cells start up to ~11pt left of their header label
_ROW_TOL = 5.0         # y-center distance that still counts as the same row

_ENTRY_RE = re.compile(r"^\s*(\d{1,4})\b\s*(.*)$", re.DOTALL)
_DATE_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\b\s*(.*)$", re.DOTALL)


def _page_lines(page):
    from pdfminer.layout import LTTextContainer, LTTextLine
    for el in page:
        if isinstance(el, LTTextContainer):
            for ln in el:
                if isinstance(ln, LTTextLine):
                    yield ln


def _header_x0(lines) -> dict[str, float]:
    found: dict[str, float] = {}
    for ln in lines:
        text = ln.get_text().strip().upper()
        for token, col in _HEADER_TOKENS:
            if text.startswith(token) and col not in found and len(text) <= 24:
                found[col] = ln.x0
    return found


def _bands(x0map: dict[str, float]) -> list[tuple[str, float, float]]:
    """[(col, x_start, x_end)] ordered left→right from header x0 positions."""
    merged = dict(_DEFAULT_X0)
    merged.update(x0map)
    ordered = sorted(merged.items(), key=lambda kv: kv[1])
    out = []
    for i, (col, x0) in enumerate(ordered):
        start = x0 - _BAND_PAD if i else 0.0
        end = ordered[i + 1][1] - _BAND_PAD if i + 1 < len(ordered) else float("inf")
        out.append((col, start, end))
    return out


def _col_for(x: float, bands) -> str:
    for col, start, end in bands:
        if start <= x < end:
            return col
    return bands[-1][0]


def _bucket_page(lines, bands) -> list[dict]:
    """Bucket every pdfminer text line on the page into (row, column) cells.

    A line is assigned to the column whose band contains its LEFT edge (cells
    start a few points left of their header label, hence _BAND_PAD). A line
    that pdfminer merged across columns ("7/7/2026 297 KOONTS LOOP",
    "61 WASHGUYS 5 LLC") is split later on its leading token by _clean_rows.
    """
    items = []
    for ln in lines:
        text = re.sub(r"\s+", " ", ln.get_text()).strip()
        if not text:
            continue
        items.append(((ln.y0 + ln.y1) / 2, ln.x0, text))
    if not items:
        return []
    items.sort(key=lambda t: (-t[0], t[1]))
    rows: list[dict] = []
    for yc, x0, text in items:
        if rows and abs(rows[-1]["_y"] - yc) <= _ROW_TOL:
            row = rows[-1]
        else:
            row = {"_y": yc, "_cells": {col: [] for col in COLUMNS}}
            rows.append(row)
        row["_cells"][_col_for(x0 + 1.0, bands)].append((x0, text))
    out = []
    for row in rows:
        cells = {}
        for col, parts in row["_cells"].items():
            parts.sort(key=lambda p: p[0])
            cells[col] = " ".join(p[1] for p in parts).strip()
        out.append(cells)
    return out


def _clean_rows(cells_rows: list[dict]) -> list[dict]:
    """Keep rows keyed on an integer ENTRY NO; split merged cells on their leading token."""
    rows = []
    for cells in cells_rows:
        m = _ENTRY_RE.match(cells.get("entry") or "")
        if not m:
            continue
        entry = int(m.group(1))
        owner = (m.group(2) + " " + (cells.get("owner") or "")).strip()
        created_raw = cells.get("created") or ""
        address = cells.get("address") or ""
        dm = _DATE_RE.match(created_raw)
        if dm:
            created = dm.group(1)
            if dm.group(2):
                address = (dm.group(2) + " " + address).strip()
        else:
            created = ""
            if created_raw:
                address = (created_raw + " " + address).strip()
        # the date sometimes lands in the address band on wide fonts
        if not created:
            am = _DATE_RE.match(address)
            if am:
                created, address = am.group(1), am.group(2).strip()
        city = cells.get("city") or ""
        if address.upper().startswith("NO ADDRESS"):
            address = address[10:].strip()
        if city.upper().startswith("NO CITY"):
            city = ""
        if address and not city:
            up = address.upper()
            for kc in _KNOWN_CITIES:
                if up.endswith(" " + kc):
                    address, city = address[: -len(kc)].strip(), kc
                    break
        rows.append({
            "entry": entry,
            "owner": owner,
            "file_no": (cells.get("file_no") or "").strip(),
            "created": created,
            "address": address.strip(),
            "city": city.strip(),
            "legal": (cells.get("legal") or "").strip(),
        })
    return rows


def extract_index_rows(pdf_bytes: bytes) -> tuple[list[dict], dict]:
    """Layout-parse an index PDF into row dicts (sorted by ENTRY NO) + self-check meta."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams

    rows: list[dict] = []
    bands = None
    pages = 0
    for page in extract_pages(io.BytesIO(pdf_bytes), laparams=LAParams()):
        pages += 1
        lines = list(_page_lines(page))
        hdr = _header_x0(lines)
        if len(hdr) >= 4 or bands is None:
            bands = _bands(hdr)
        rows.extend(_clean_rows(_bucket_page(lines, bands)))

    rows.sort(key=lambda r: r["entry"])
    entries = [r["entry"] for r in rows]
    n = len(rows)
    meta = {
        "pages": pages,
        "index_rows": n,
        "index_entry_check": bool(n) and sorted(set(entries)) == list(range(1, n + 1)),
        "no_address": sum(1 for r in rows if not r["address"]),
        "no_city": sum(1 for r in rows if not r["city"]),
    }
    return rows, meta


def parse_index_pdf_bytes(pdf_bytes: bytes, sale_month: str, sale_year: int) -> tuple[list[NoticeData], dict]:
    """Parse an index PDF into NoticeData records + self-check meta."""
    rows, meta = extract_index_rows(pdf_bytes)
    if not meta["index_entry_check"]:
        logger.warning(
            "Williamson %s %d index: ENTRY NO self-check failed (%d rows, entries %s..%s) — "
            "returning rows anyway", sale_month, sale_year, meta["index_rows"],
            min((r["entry"] for r in rows), default="?"), max((r["entry"] for r in rows), default="?"),
        )
    sale_date = _first_tuesday(sale_month, sale_year)
    notices: list[NoticeData] = []

    from notice_parser import normalize_court_name
    from scrapers.trustee_blocklist import is_substitute_trustee

    for row in rows:
        raw_owner = row["owner"].title()
        # Drop known TX substitute trustees (filing attorneys); CAD address
        # lookup downstream will fill the real borrower from tax records.
        if not raw_owner:
            owner = ""
        elif is_substitute_trustee(raw_owner):
            logger.info(
                "Skipped substitute trustee %r on Williamson foreclosure; "
                "leaving owner_name blank for CAD lookup", raw_owner,
            )
            owner = ""
        else:
            owner = normalize_court_name(raw_owner)
        filed_date = ""
        try:
            filed_date = datetime.strptime(row["created"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
        notice = NoticeData(
            notice_type="foreclosure",
            county=COUNTY,
            state="TX",
            date_added=filed_date or datetime.now().strftime("%Y-%m-%d"),
            auction_date=sale_date,
            auction_date_source="folder_month" if sale_date else "",
            address=row["address"].title(),
            city=row["city"].title(),
            owner_name=owner,
            source_url=f"{BASE_URL}/{sale_month}/files.aspx",
        )
        # PAPERTRAIL: pristine pre-flip name; skipped when blanked by the blocklist.
        if owner:
            notice.tax_owner_name = raw_owner
        notice.raw_text = (
            f"ENTRY {row['entry']} | {row['owner']} | FILE {row['file_no']} | "
            f"{row['created']} | {row['address']} | {row['city']} | {row['legal']}"
        )
        notices.append(notice)
    return notices, meta


def _parse_index_pdf(text: str, sale_month: str, sale_year: int) -> list[NoticeData]:
    """Removed 2026-09-09 — the text-block parser could not survive the clerk's
    layout change. Use parse_index_pdf_bytes(pdf_bytes, ...)."""
    raise NotImplementedError(
        "foreclosure_wilco._parse_index_pdf(text) is gone; use parse_index_pdf_bytes(pdf_bytes, month, year)"
    )


# ── Scraper ───────────────────────────────────────────────────────────
def _new_meta() -> dict:
    return {
        "months": [],
        "index_rows": 0,
        "index_entry_check": True,
        "months_without_index": 0,
        "pdfs_total": 0,
        "pdfs_unchanged": 0,
        "pdfs_new": 0,
        "pdfs_ocr": 0,
        "ocr_secs": 0.0,
        "budget_exhausted": False,
        "returned": 0,
        "kept": 0,
    }


@register("Williamson", "foreclosure")
class WilliamsonForeclosureScraper:
    """Scrape foreclosure notices from Williamson County trustee sales postings."""

    def __init__(self, **_kw):
        self.last_meta: dict = {}

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        now = datetime.now()
        # Determine which SALE months to scrape
        if mode == "historical":
            months_to_check = []
            for i in range(12):
                dt = now - timedelta(days=30 * i)
                months_to_check.append((MONTHS[dt.month - 1], dt.year))
        else:
            # Current month + next month (notices posted 21 days before sale)
            months_to_check = [(MONTHS[now.month - 1], now.year)]
            next_month = now.month % 12 + 1
            next_year = now.year + (1 if next_month == 1 else 0)
            months_to_check.append((MONTHS[next_month - 1], next_year))

        logger.info(
            "Williamson foreclosure scrape: mode=%s, months=%s",
            mode, [f"{m} {y}" for m, y in months_to_check],
        )

        budget = budget_from_env(OCR_BUDGET_ENV, OCR_BUDGET_DEFAULT_S, "Williamson foreclosure")
        state = pdf_state.load_state(COUNTY)
        self.last_meta = _new_meta()
        meta = self.last_meta
        meta["months"] = [f"{m} {y}" for m, y in months_to_check]
        keep_months = {f"{y:04d}-{MONTHS.index(m) + 1:02d}" for m, y in months_to_check}

        all_notices: list[NoticeData] = []
        fetch_failures = 0
        for month_name, year in months_to_check:
            files = _get_month_file_list(month_name)
            if files is None:
                fetch_failures += 1
                continue
            if not files:
                logger.info("No files posted for %s %d", month_name, year)
                continue
            idx_file = _find_index_pdf(files)
            if idx_file:
                logger.info("Processing %s %d index: %s", month_name, year, idx_file)
                data = _download_pdf_bytes(month_name, idx_file)
                if not data:
                    continue
                try:
                    notices, imeta = parse_index_pdf_bytes(data, month_name, year)
                except Exception as e:
                    logger.warning("Williamson %s %d index parse failed: %s", month_name, year, e)
                    continue
                meta["index_rows"] += imeta["index_rows"]
                meta["returned"] += imeta["index_rows"]
                meta["index_entry_check"] = meta["index_entry_check"] and imeta["index_entry_check"]
                all_notices.extend(notices)
                logger.info(
                    "  %s %d: %d foreclosure notices from index (%d rows, entry check %s, "
                    "%d without address)",
                    month_name, year, len(notices), imeta["index_rows"],
                    "ok" if imeta["index_entry_check"] else "FAILED", imeta["no_address"],
                )
            else:
                meta["months_without_index"] += 1
                notice_files = [f.split("/")[-1] for f in files if NOTICE_FILE_RE.match(f.split("/")[-1])]
                logger.info(
                    "No index PDF for %s %d yet — reading %d notice PDFs under the OCR budget "
                    "(%.0fs left)", month_name, year, len(notice_files), budget.remaining(),
                )
                all_notices.extend(
                    self._scrape_notice_pdfs(month_name, year, notice_files, state, budget, now)
                )
            if max_notices and len(all_notices) >= max_notices:
                all_notices = all_notices[:max_notices]
                break

        if fetch_failures and fetch_failures == len(months_to_check):
            from scrapers import ScraperError
            raise ScraperError(
                f"Williamson foreclosure: every month listing fetch failed "
                f"({fetch_failures}/{len(months_to_check)}) — site down or URL changed"
            )

        meta["kept"] = len(all_notices)
        meta["ocr_secs"] = round(meta["ocr_secs"], 1)

        # Bookkeeping never breaks a scrape; smoke runs (max_notices) never write state.
        if not max_notices:
            try:
                pdf_state.prune(state, keep_months)
                pdf_state.commit_run(COUNTY, state, dict(meta))
            except Exception as e:
                logger.warning("Williamson foreclosure: state commit failed: %s", e)

        logger.info(
            "Williamson foreclosure scrape complete: %d notices (index rows %d; notice PDFs %d: "
            "%d replayed, %d read, %d OCR'd in %.0fs%s)",
            len(all_notices), meta["index_rows"], meta["pdfs_total"], meta["pdfs_unchanged"],
            meta["pdfs_new"], meta["pdfs_ocr"], meta["ocr_secs"],
            "; budget exhausted — unfinished PDFs resume next run" if meta["budget_exhausted"] else "",
        )
        return all_notices

    # ── No-index months: individual scanned notice PDFs ───────────────
    def _scrape_notice_pdfs(self, month_name: str, year: int, files: list[str],
                            state: dict, budget, now: datetime) -> list[NoticeData]:
        meta = self.last_meta
        sale_date = _first_tuesday(month_name, year)
        month_key = sale_date[:7]
        today = now.strftime("%Y-%m-%d")

        def _filed(fn: str) -> str:
            m = NOTICE_FILE_RE.match(fn)
            return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else today

        # unfinished / unseen first (newest filing first), then known ones
        pending, known = [], []
        for fn in files:
            entry = state["pdfs"].get(f"{month_name}/{fn}")
            (known if entry and entry.get("complete") else pending).append(fn)
        pending.sort(key=_filed, reverse=True)
        ordered = pending + known
        meta["pdfs_total"] += len(ordered)

        out: list[NoticeData] = []
        for fn in ordered:
            key = f"{month_name}/{fn}"
            url = _pdf_url(month_name, fn)
            entry = state["pdfs"].get(key)

            # Cheap replay: HEAD size matches a complete entry → no download.
            if entry and entry.get("complete") and int(entry.get("parser_version") or 0) == PARSER_VERSION:
                if pdf_state.size_matches(entry, _head_size(month_name, fn)):
                    pdf_state.touch(state, key, today)
                    meta["pdfs_unchanged"] += 1
                    meta["returned"] += int(entry.get("returned") or 0)
                    out.extend(pdf_state.replay(entry, COUNTY, url))
                    continue

            data = _download_pdf_bytes(month_name, fn)
            if not data or len(data) < 100:
                continue
            sha256, size = pdf_state.fingerprint(data)
            if pdf_state.is_unchanged(entry, sha256, size, PARSER_VERSION) and entry.get("complete"):
                pdf_state.touch(state, key, today)
                meta["pdfs_unchanged"] += 1
                meta["returned"] += int(entry.get("returned") or 0)
                out.extend(pdf_state.replay(entry, COUNTY, url))
                continue

            meta["pdfs_new"] += 1
            resume = entry if (entry and entry.get("sha256") == sha256 and not entry.get("complete")) else None
            text = text_layer(data)
            text_source = "layer"
            pages_total = pages_done = 0
            complete = True
            partial_text = ""
            if not has_text(text):
                if budget.exhausted():
                    # Don't even open it: remember it as pending so ordering stays right.
                    meta["budget_exhausted"] = True
                    if not resume:
                        pdf_state.record_pdf(
                            state, key, sha256=sha256, size=size, label=fn, month=month_key,
                            parser_version=PARSER_VERSION, text_source="ocr", pages_total=0,
                            pages_done=0, complete=False, partial_text="", returned=0,
                            notices=[], today=today,
                        )
                    continue
                start = int(resume.get("pages_done") or 0) if resume else 0
                prefix = (resume.get("partial_text") or "") if resume else ""
                res = ocr_pdf_bytes(
                    data, budget, start_page=start, prefix_text=prefix, rotate=True,
                    label=f"Williamson {fn}",
                )
                meta["ocr_secs"] += res.secs
                if res.pages_done > start:
                    meta["pdfs_ocr"] += 1
                text, text_source = res.text, "ocr"
                pages_total, pages_done, complete = res.pages_total, res.pages_done, res.complete
                if res.engine_error:
                    # OCR engine broken (no Tesseract?) — keep the PDF pending, never seal it.
                    if not meta.get("ocr_engine_error"):
                        logger.error("  %s: OCR engine failed (%s) — PDFs stay pending", fn, res.engine_error)
                    meta["ocr_engine_error"] = res.engine_error
                    partial_text = (resume.get("partial_text") or "") if resume else ""
                elif not complete:
                    meta["budget_exhausted"] = True
                    partial_text = text
                if not has_text(text) or res.engine_error:
                    if complete:
                        text_source = "none"
                        logger.warning("  %s: no text layer and OCR returned nothing (%d page(s))", fn, pages_total)
                    pdf_state.record_pdf(
                        state, key, sha256=sha256, size=size, label=fn, month=month_key,
                        parser_version=PARSER_VERSION, text_source=text_source,
                        pages_total=pages_total, pages_done=pages_done, complete=complete,
                        partial_text=partial_text, returned=0, notices=[], today=today,
                    )
                    continue

            meta["returned"] += 1
            notice = parse_trustee_notice_text(
                text, county=COUNTY, source_url=url, fallback_sale_date=sale_date,
                date_added=_filed(fn),
            )
            notices = [notice] if notice else []
            pdf_state.record_pdf(
                state, key, sha256=sha256, size=size, label=fn, month=month_key,
                parser_version=PARSER_VERSION, text_source=text_source,
                pages_total=pages_total, pages_done=pages_done, complete=complete,
                partial_text=partial_text, returned=1, notices=notices, today=today,
            )
            logger.info(
                "  %s: %s%s", fn,
                f"{notice.address or '(no address)'} / {notice.owner_name or '(no owner)'}" if notice else "no usable notice text",
                f" — partial {pages_done}/{pages_total} pages, resumes next run" if not complete else "",
            )
            out.extend(notices)
        return out

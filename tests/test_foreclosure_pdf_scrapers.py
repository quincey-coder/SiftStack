"""Flow tests for the PDF-memory foreclosure scrapers (no network, no OCR).

Bell: `_process_pdf` reads a PDF once (text layer or OCR), remembers it, and
replays it on the next call without spending the OCR budget; an exhausted
budget leaves the PDF resumable instead of silently skipping it.

Run: PYTHONPATH=src python tests/test_foreclosure_pdf_scrapers.py   (or pytest)
"""

import asyncio
import os
import sys
from datetime import datetime

os.environ["APIFY_IS_AT_HOME"] = "1"   # in-memory state, never touches disk
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import foreclosure_bell as fb  # noqa: E402
from scrapers import pdf_notice_state as st  # noqa: E402
from scrapers.pdf_ocr import OcrBudget, OcrResult  # noqa: E402

NOTICE = """NOTICE OF SUBSTITUTE TRUSTEE'S SALE
WHEREAS, by a Deed of Trust executed by Maria Lopez, conveying to the Trustee
the real property commonly known as 401 Pecan Drive, Temple, Texas 76502, and
WHEREAS default has occurred, notice is given that on October 6, 2026, being
the first Tuesday of said month, the Substitute Trustee will sell the property
at public auction at the Bell County courthouse. Substitute Trustee: A. Zavala.
"""
NOW = datetime(2026, 9, 9)


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def body(self):
        return self._body


class _Ctx:
    def __init__(self, body):
        self.body_bytes = body
        self.gets = 0
        self.request = self

    async def get(self, href):
        self.gets += 1
        return _Resp(self.body_bytes)


def _fresh_state():
    st._APIFY_STATE_CACHE.clear()
    st.inject_apify_state("Bell", None)
    return st.load_state("Bell")


def _run(coro):
    return asyncio.run(coro)


def test_text_layer_pdf_parsed_then_replayed():
    calls = []
    fb.text_layer = lambda b: NOTICE * 2
    fb.ocr_pdf_bytes = lambda *a, **k: calls.append(1) or OcrResult("", 0, 0, False, 0)
    s = fb.BellForeclosureScraper()
    s.last_meta = fb._new_meta()
    state = _fresh_state()
    ctx = _Ctx(b"%PDF-1 fake bytes " * 10)
    info = {"href": "http://bell/Sept%205.pdf", "text": "September Foreclosure Notices (5)"}
    budget = OcrBudget(900)

    n1 = _run(s._process_pdf(ctx, info, state, budget, NOW))
    assert len(n1) == 2 and n1[0].address == "401 Pecan Drive" and n1[0].city == "Temple"
    assert n1[0].owner_name == "Maria Lopez" and n1[0].auction_date == "2026-10-06"
    assert n1[0].county == "Bell" and n1[0].source_url == info["href"]
    assert s.last_meta["pdfs_new"] == 1 and s.last_meta["returned"] == 2
    entry = state["pdfs"][info["href"]]
    assert entry["complete"] and entry["text_source"] == "layer" and len(entry["records"]) == 2

    n2 = _run(s._process_pdf(ctx, info, state, budget, NOW))
    assert len(n2) == 2 and n2[0].address == "401 Pecan Drive"
    assert s.last_meta["pdfs_unchanged"] == 1 and s.last_meta["returned"] == 4
    assert calls == []                       # never OCR'd
    assert ctx.gets == 2                     # downloaded both times (fingerprint check)


def test_changed_bytes_reparsed():
    fb.text_layer = lambda b: NOTICE
    s = fb.BellForeclosureScraper()
    s.last_meta = fb._new_meta()
    state = _fresh_state()
    info = {"href": "http://bell/Sept%206.pdf", "text": "September Foreclosure Notices (6)"}
    _run(s._process_pdf(_Ctx(b"v1" * 60), info, state, OcrBudget(900), NOW))
    _run(s._process_pdf(_Ctx(b"v2" * 60), info, state, OcrBudget(900), NOW))
    assert s.last_meta["pdfs_new"] == 2 and s.last_meta["pdfs_unchanged"] == 0


def test_ocr_budget_exhausted_is_resumable_not_skipped():
    fb.text_layer = lambda b: ""
    seen = {}

    def fake_ocr(pdf_bytes, budget, *, start_page=0, prefix_text="", label="", **kw):
        seen["start"] = start_page
        seen["prefix"] = prefix_text
        if budget.limit_s < 1:
            return OcrResult(prefix_text, 3, start_page, False, 0.0)     # nothing possible
        # one page per call
        text = (prefix_text + "\n" if prefix_text else "") + f"PAGE{start_page + 1} " + NOTICE
        done = start_page + 1
        return OcrResult(text, 3, done, done >= 3, 5.0)

    fb.ocr_pdf_bytes = fake_ocr
    s = fb.BellForeclosureScraper()
    s.last_meta = fb._new_meta()
    state = _fresh_state()
    info = {"href": "http://bell/Oct%201.pdf", "text": "October Foreclosure Notices (1)"}
    ctx = _Ctx(b"scan" * 50)

    out = _run(s._process_pdf(ctx, info, state, OcrBudget(0.0), NOW))
    assert out == [] and s.last_meta["budget_exhausted"] and s.last_meta["pdfs_ocr"] == 0
    e = state["pdfs"][info["href"]]
    assert not e["complete"] and e["pages_done"] == 0

    s.last_meta = fb._new_meta()
    out = _run(s._process_pdf(ctx, info, state, OcrBudget(900), NOW))
    assert seen["start"] == 0
    assert len(out) == 1 and s.last_meta["pdfs_ocr"] == 1 and s.last_meta["budget_exhausted"]
    e = state["pdfs"][info["href"]]
    assert e["pages_done"] == 1 and not e["complete"] and e["partial_text"].startswith("PAGE1")

    s.last_meta = fb._new_meta()
    _run(s._process_pdf(ctx, info, state, OcrBudget(900), NOW))
    assert seen["start"] == 1 and seen["prefix"].startswith("PAGE1")
    s.last_meta = fb._new_meta()
    out = _run(s._process_pdf(ctx, info, state, OcrBudget(900), NOW))
    e = state["pdfs"][info["href"]]
    assert e["complete"] and e["pages_done"] == 3 and e["partial_text"] == ""
    assert not s.last_meta["budget_exhausted"]
    # 3 notice copies in the text (one per page)
    assert len(out) == 3 and e["returned"] == 3

    s.last_meta = fb._new_meta()
    out = _run(s._process_pdf(ctx, info, state, OcrBudget(900), NOW))
    assert s.last_meta["pdfs_unchanged"] == 1 and len(out) == 3


def test_ocr_engine_failure_keeps_pdf_pending():
    fb.text_layer = lambda b: ""
    fb.ocr_pdf_bytes = lambda *a, **k: OcrResult("", 2, 0, False, 0.5, engine_error="RuntimeError: no tesseract")
    s = fb.BellForeclosureScraper()
    s.last_meta = fb._new_meta()
    state = _fresh_state()
    info = {"href": "http://bell/Oct%209.pdf", "text": "October Foreclosure Notices (9)"}
    out = _run(s._process_pdf(_Ctx(b"scan" * 50), info, state, OcrBudget(900), NOW))
    assert out == []
    e = state["pdfs"][info["href"]]
    assert not e["complete"] and e["pages_done"] == 0 and e["text_source"] == "ocr"
    assert s.last_meta["ocr_engine_error"].startswith("RuntimeError")
    assert not s.last_meta["budget_exhausted"]


def test_wilco_ocr_engine_failure_keeps_pdf_pending():
    files = ["09-04-2026_File_002.pdf"]
    fw._download_pdf_bytes = lambda month, fn: b"scan" * 40
    fw._head_size = lambda month, fn: 160
    fw.text_layer = lambda b: ""
    fw.ocr_pdf_bytes = lambda *a, **k: OcrResult("", 3, 0, False, 0.5, engine_error="RuntimeError: no tesseract")
    s = fw.WilliamsonForeclosureScraper()
    s.last_meta = fw._new_meta()
    state = _wilco_state()
    out = s._scrape_notice_pdfs("October", 2026, files, state, OcrBudget(600), NOW)
    assert out == []
    e = state["pdfs"]["October/09-04-2026_File_002.pdf"]
    assert not e["complete"] and e["pages_done"] == 0
    assert s.last_meta["ocr_engine_error"] and not s.last_meta["budget_exhausted"]


def test_order_pdfs_pending_first_newest_first():
    state = _fresh_state()
    state["pdfs"]["a"] = {"complete": True}
    state["pdfs"]["b"] = {"complete": False}
    infos = [{"href": "a"}, {"href": "b"}, {"href": "c"}]
    assert [i["href"] for i in fb._order_pdfs(infos, state)] == ["c", "b", "a"]


def test_back_compat_wrapper():
    n = fb._parse_notice_text(NOTICE)
    assert n is not None and n.county == "Bell" and n.address == "401 Pecan Drive"
    assert fb._first_tuesday(2026, 10).day == 6


# ── Williamson: index-less month → individual notice PDFs ─────────────
from scrapers import foreclosure_wilco as fw  # noqa: E402

WNOTICE = NOTICE.replace("Temple, Texas 76502", "Hutto, Texas 78634").replace("Maria Lopez", "Carl Ray")


def _wilco_state():
    st._APIFY_STATE_CACHE.clear()
    st.inject_apify_state("Williamson", None)
    return st.load_state("Williamson")


def test_wilco_notice_pdfs_ocr_remember_replay_via_head():
    downloads, heads, ocr_calls = [], [], []
    files = ["07-10-2026_File_013.pdf", "09-04-2026_File_012.pdf"]
    body = {f: (f + " bytes ").encode() * 20 for f in files}

    fw._download_pdf_bytes = lambda month, fn: downloads.append(fn) or body[fn]
    fw._head_size = lambda month, fn: heads.append(fn) or len(body[fn])
    fw.text_layer = lambda b: ""

    def fake_ocr(pdf_bytes, budget, *, start_page=0, prefix_text="", rotate=False, label="", **kw):
        ocr_calls.append(label)
        return OcrResult(WNOTICE, 1, 1, True, 2.0)

    fw.ocr_pdf_bytes = fake_ocr
    s = fw.WilliamsonForeclosureScraper()
    s.last_meta = fw._new_meta()
    state = _wilco_state()
    out = s._scrape_notice_pdfs("October", 2026, files, state, OcrBudget(600), NOW)
    assert len(out) == 2
    # newest filing first
    assert downloads == ["09-04-2026_File_012.pdf", "07-10-2026_File_013.pdf"], downloads
    n = [x for x in out if x.source_url.endswith("File_013.pdf")][0]
    assert n.county == "Williamson" and n.address == "401 Pecan Drive" and n.city == "Hutto"
    assert n.owner_name == "Carl Ray" and n.auction_date == "2026-10-06"
    assert n.date_added == "2026-07-10"          # filing date from the filename
    assert s.last_meta["pdfs_ocr"] == 2 and s.last_meta["returned"] == 2
    assert state["pdfs"]["October/07-10-2026_File_013.pdf"]["complete"]

    # run 2: HEAD size matches → replay without download or OCR
    s.last_meta = fw._new_meta()
    downloads.clear(); ocr_calls.clear(); heads.clear()
    out2 = s._scrape_notice_pdfs("October", 2026, files, state, OcrBudget(600), NOW)
    assert len(out2) == 2 and downloads == [] and ocr_calls == [] and len(heads) == 2
    assert s.last_meta["pdfs_unchanged"] == 2 and s.last_meta["returned"] == 2
    assert out2[0].address == "401 Pecan Drive"

    # run 3: HEAD size changed → download; bytes changed → re-OCR
    body[files[0]] = b"new scan " * 30
    fw._head_size = lambda month, fn: len(body[fn])
    s.last_meta = fw._new_meta()
    downloads.clear(); ocr_calls.clear()
    s._scrape_notice_pdfs("October", 2026, files, state, OcrBudget(600), NOW)
    assert downloads == [files[0]] and len(ocr_calls) == 1
    assert s.last_meta["pdfs_new"] == 1 and s.last_meta["pdfs_unchanged"] == 1


def test_wilco_exhausted_budget_records_pending_and_resumes():
    files = ["09-04-2026_File_001.pdf"]
    fw._download_pdf_bytes = lambda month, fn: b"scan" * 40
    fw._head_size = lambda month, fn: 160
    fw.text_layer = lambda b: ""
    seen = {}

    def fake_ocr(pdf_bytes, budget, *, start_page=0, prefix_text="", rotate=False, label="", **kw):
        seen["start"] = start_page
        return OcrResult(prefix_text + "\n" + WNOTICE, 2, start_page + 1, start_page + 1 >= 2, 3.0)

    fw.ocr_pdf_bytes = fake_ocr
    s = fw.WilliamsonForeclosureScraper()
    s.last_meta = fw._new_meta()
    state = _wilco_state()
    b = OcrBudget(1.0); b.charge(2.0)                      # already spent
    out = s._scrape_notice_pdfs("October", 2026, files, state, b, NOW)
    assert out == [] and s.last_meta["budget_exhausted"] and "start" not in seen
    e = state["pdfs"]["October/09-04-2026_File_001.pdf"]
    assert not e["complete"] and e["pages_done"] == 0

    s.last_meta = fw._new_meta()
    out = s._scrape_notice_pdfs("October", 2026, files, state, OcrBudget(600), NOW)
    assert seen["start"] == 0 and len(out) == 1 and s.last_meta["budget_exhausted"]
    s.last_meta = fw._new_meta()
    out = s._scrape_notice_pdfs("October", 2026, files, state, OcrBudget(600), NOW)
    assert seen["start"] == 1 and len(out) == 1 and not s.last_meta["budget_exhausted"]
    e = state["pdfs"]["October/09-04-2026_File_001.pdf"]
    assert e["complete"] and e["pages_done"] == 2 and e["partial_text"] == ""


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

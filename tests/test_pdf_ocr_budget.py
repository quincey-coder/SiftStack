"""OcrBudget / ocr_pdf_bytes resume semantics (no tesseract, no pypdfium2).

Run: PYTHONPATH=src python tests/test_pdf_ocr_budget.py   (or pytest)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import pdf_ocr  # noqa: E402
from scrapers.pdf_ocr import OcrBudget, budget_from_env, ocr_pdf_bytes  # noqa: E402


class _FakeBitmap:
    def to_pil(self):
        return object()


class _FakePage:
    def render(self, scale):
        return _FakeBitmap()


class _FakePdf:
    def __init__(self, n):
        self.n = n
        self.closed = False

    def __len__(self):
        return self.n

    def __getitem__(self, i):
        return _FakePage()

    def close(self):
        self.closed = True


def _patch(n_pages, ocr_calls, per_page_secs=0.0):
    pdf_ocr._open_pdf = lambda b: _FakePdf(n_pages)

    def fake_ocr(image, psm, rotate):
        ocr_calls.append((psm, rotate))
        return "x" * 60

    pdf_ocr._ocr_pil = fake_ocr


def test_zero_budget_does_nothing():
    calls = []
    _patch(3, calls)
    r = ocr_pdf_bytes(b"%PDF", OcrBudget(0.0))
    assert r.pages_done == 0 and not r.complete and r.pages_total == 3
    assert calls == []


def test_full_budget_completes():
    calls = []
    _patch(3, calls)
    b = OcrBudget(100.0)
    r = ocr_pdf_bytes(b"%PDF", b, rotate=True)
    assert r.complete and r.pages_done == 3
    assert len(calls) == 3 and calls[0] == (3, True)
    assert r.text.count("x" * 60) == 3


def test_resume_from_start_page_with_prefix():
    calls = []
    _patch(3, calls)
    r = ocr_pdf_bytes(b"%PDF", OcrBudget(100.0), start_page=2, prefix_text="EARLIER")
    assert r.pages_done == 3 and r.complete
    assert len(calls) == 1
    assert r.text.startswith("EARLIER")


def test_shared_budget_accumulates_and_stops():
    calls = []
    _patch(2, calls)
    b = OcrBudget(1.0)
    b.charge(1.5)                       # already over
    r = ocr_pdf_bytes(b"%PDF", b)
    assert r.pages_done == 0 and not r.complete and calls == []
    assert b.exhausted() and b.remaining() == 0.0


def test_budget_checked_before_each_page():
    calls = []
    _patch(3, calls)
    b = OcrBudget(0.5)
    # charge inside the fake OCR so the 2nd page sees an exhausted budget
    def slow_ocr(image, psm, rotate):
        calls.append(1)
        b.charge(1.0)
        return "y" * 60
    pdf_ocr._ocr_pil = slow_ocr
    r = ocr_pdf_bytes(b"%PDF", b)
    assert r.pages_done == 1 and not r.complete and len(calls) == 1


def test_engine_failure_reports_no_progress():
    calls = []
    _patch(4, calls)

    def broken(image, psm, rotate):
        calls.append(1)
        raise RuntimeError("tesseract is not installed")

    pdf_ocr._ocr_pil = broken
    b = OcrBudget(100.0)
    r = ocr_pdf_bytes(b"%PDF", b, start_page=1, prefix_text="KEEP")
    assert r.engine_error.startswith("RuntimeError") and not r.complete
    assert r.pages_done == 1 and r.pages_total == 4      # no progress past start_page
    assert len(calls) == 2                               # stopped early, budget preserved
    assert r.text == "KEEP"


def test_single_page_failure_among_successes_is_not_engine_error():
    calls = []
    _patch(3, calls)
    state = {"n": 0}

    def flaky(image, psm, rotate):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("bad page")
        return "z" * 60

    pdf_ocr._ocr_pil = flaky
    r = ocr_pdf_bytes(b"%PDF", OcrBudget(100.0))
    assert r.complete and r.pages_done == 3 and r.engine_error == ""


def test_open_failure_is_soft():
    def boom(b):
        raise RuntimeError("bad pdf")
    pdf_ocr._open_pdf = boom
    r = ocr_pdf_bytes(b"junk", OcrBudget(10.0), prefix_text="P")
    assert r.text == "P" and r.pages_total == 0 and not r.complete


def test_budget_from_env():
    os.environ["SIFT_TEST_OCR_BUDGET"] = "12.5"
    try:
        assert budget_from_env("SIFT_TEST_OCR_BUDGET", 900, "t").limit_s == 12.5
    finally:
        os.environ["SIFT_TEST_OCR_BUDGET"] = "abc"
        assert budget_from_env("SIFT_TEST_OCR_BUDGET", 900, "t").limit_s == 900
        del os.environ["SIFT_TEST_OCR_BUDGET"]
    assert budget_from_env("SIFT_TEST_OCR_BUDGET", 600, "t").limit_s == 600


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

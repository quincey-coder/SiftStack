"""Budgeted OCR for scanned notice PDFs — shared by Bell + Williamson foreclosure.

Promoted out of foreclosure_bell (2026-09-09). The old version kept a
module-global elapsed counter, so (a) every daily run re-spent the whole
budget on the same first PDFs, (b) a `scrape_targets` retry re-entered with
the budget already gone, and (c) a second county could not have its own
budget. Here the budget is an object created per `scrape()` call, the OCR
result reports how many pages it managed, and a caller can RESUME a PDF at
`start_page` on the next run (see scrapers/pdf_notice_state.py).

Pipeline per page (unchanged from Bell's tuning): render at 150 DPI →
grayscale → medianBlur(3) → Otsu threshold → tesseract psm 3. The old
200-DPI + bilateralFilter path was ~1-3 min/page and timed out whole runs.
"""

from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS = 50   # below this a page/PDF is treated as "no usable text"


@dataclass
class OcrBudget:
    """Cumulative wall-clock budget for one scraper run."""

    limit_s: float
    elapsed_s: float = 0.0
    label: str = ""

    def remaining(self) -> float:
        return max(0.0, self.limit_s - self.elapsed_s)

    def exhausted(self) -> bool:
        return self.elapsed_s >= self.limit_s

    def charge(self, secs: float) -> None:
        self.elapsed_s += max(0.0, secs)


def budget_from_env(env_var: str, default_s: float, label: str) -> OcrBudget:
    """OcrBudget from an env override (positive number) else the default."""
    raw = os.getenv(env_var, "").strip()
    limit = default_s
    if raw:
        try:
            v = float(raw)
            if v >= 0:
                limit = v
        except ValueError:
            logger.warning("%s: ignoring non-numeric %s=%r", label, env_var, raw)
    return OcrBudget(limit_s=limit, label=label)


@dataclass
class OcrResult:
    text: str
    pages_total: int
    pages_done: int          # absolute count incl. pages done in earlier runs
    complete: bool           # pages_done == pages_total
    secs: float              # seconds spent THIS call
    engine_error: str = ""   # set when EVERY attempted page raised (e.g. no Tesseract)


def text_layer(pdf_bytes: bytes) -> str:
    """pdfminer text layer, '' on any failure (scans have none)."""
    try:
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(pdf_bytes)) or ""
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("text_layer failed: %s", e)
        return ""


def has_text(text: str | None) -> bool:
    return bool(text) and len(text.strip()) >= MIN_TEXT_CHARS


# ── Seams (monkeypatched by tests) ───────────────────────────────────
def _open_pdf(pdf_bytes: bytes):
    """Return a pypdfium2 document (len() + indexing + page.render(scale))."""
    import pypdfium2 as pdfium
    return pdfium.PdfDocument(pdf_bytes)


def _ocr_pil(image, psm: int, rotate: bool) -> str:
    """Preprocess a rendered page and OCR it."""
    import cv2
    import numpy as np
    from PIL import Image

    from image_utils import fix_rotation, ocr_page

    if rotate:
        try:
            image = fix_rotation(image)
        except Exception as e:
            logger.debug("fix_rotation failed: %s", e)
    gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    filtered = cv2.medianBlur(gray, 3)
    _, binary = cv2.threshold(filtered, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return ocr_page(Image.fromarray(binary), psm=psm) or ""


def ocr_pdf_bytes(
    pdf_bytes: bytes,
    budget: OcrBudget,
    *,
    start_page: int = 0,
    prefix_text: str = "",
    dpi: int = 150,
    psm: int = 3,
    rotate: bool = False,
    label: str = "",
) -> OcrResult:
    """OCR pages [start_page, n) until done or the budget runs out.

    `prefix_text` is the text already OCR'd for this PDF in an earlier run
    (resume support); it is prepended so the caller always sees the full
    text-so-far. Never raises: failures log and return what was gathered.
    """
    tag = label or budget.label or "pdf"
    texts: list[str] = [prefix_text] if prefix_text else []
    t_call = time.time()
    pages_total = 0
    done = start_page
    try:
        pdf = _open_pdf(pdf_bytes)
    except ImportError:
        logger.warning("pypdfium2 not installed — cannot OCR scanned PDFs")
        return OcrResult("\n".join(texts), 0, done, False, 0.0)
    except Exception as e:
        logger.warning("%s: cannot open PDF for OCR: %s", tag, e)
        return OcrResult("\n".join(texts), 0, done, False, 0.0)

    attempted = failed = 0
    last_err = ""
    try:
        pages_total = len(pdf)
        for idx in range(start_page, pages_total):
            if budget.exhausted():
                logger.info(
                    "%s: OCR budget (%.0fs) exhausted at page %d/%d — resumes next run",
                    tag, budget.limit_s, idx, pages_total,
                )
                break
            t0 = time.time()
            attempted += 1
            try:
                page = pdf[idx]
                pil_image = page.render(scale=dpi / 72).to_pil()
                page_text = _ocr_pil(pil_image, psm, rotate)
            except Exception as e:
                failed += 1
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("%s: OCR failed on page %d: %s", tag, idx + 1, e)
                page_text = ""
                if failed >= 2 and failed == attempted:
                    break   # the engine itself is broken — don't burn the budget
            budget.charge(time.time() - t0)
            done = idx + 1
            if page_text:
                texts.append(page_text)
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    secs = time.time() - t_call
    engine_error = last_err if (attempted and failed == attempted) else ""
    if engine_error:
        # No page produced anything because the OCR engine failed on every try
        # (e.g. Tesseract missing). Report NO progress so the caller keeps the
        # PDF pending instead of sealing it as "complete, no text".
        done = start_page
    return OcrResult(
        text="\n".join(texts),
        pages_total=pages_total,
        pages_done=done,
        complete=not engine_error and pages_total > 0 and done >= pages_total,
        secs=secs,
        engine_error=engine_error,
    )

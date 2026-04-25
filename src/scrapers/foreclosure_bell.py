"""Bell County foreclosure scraper — monthly PDFs from county clerk.

Downloads monthly foreclosure notice PDFs from the Bell County Clerk's
website using Playwright (site requires browser-like requests).

Source: https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php
PDF pattern: "May 1.pdf" through "May N.pdf" (accumulates weekly)

Each PDF contains one or more foreclosure notices with:
  - Property address ("commonly known as ...")
  - Owner/borrower name ("executed by ...")
  - Sale date (first Tuesday of month)
  - Trustee information
"""

import io
import logging
import re
from datetime import datetime, timedelta

import cv2
import numpy as np
from PIL import Image
from playwright.async_api import async_playwright
from pdfminer.high_level import extract_text

from image_utils import fix_rotation, ocr_page
from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

BELL_FORECLOSURE_URL = "https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php"

# Regex patterns for extracting data from notice text
_ADDR_RE = re.compile(
    r"commonly\s+known\s+as\s*:?\s*"
    r"(\d{1,5}\s+[\w\s.'-]+?"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Circle|Cir|Court|Ct|Place|Pl|"
    r"Pike|Highway|Hwy|Trail|Trl|Terrace|Ter|Parkway|Pkwy|"
    r"Cove|Cv|Loop|Run|Path|Ridge|Crossing|Bend|Point|Pass)\.?)"
    r"\s*[,.]?\s*([\w\s]+?)"
    r"\s*[,.]?\s*(?:Texas|Tex\.?|TX)"
    r"\s*[,.\s]*(\d{5})?",
    re.IGNORECASE,
)

_OWNER_RE = re.compile(
    r"executed\s+by\s+(.+?)(?:\s*,\s*(?:a|conveying|to\s|grantor|whose))",
    re.IGNORECASE,
)

_SALE_DATE_RE = re.compile(
    r"(\w+\s+\d{1,2},?\s+\d{4})\s*(?:,\s*)?(?:being\s+)?(?:the\s+)?(?:first\s+)?(?:Tuesday|Tues)",
    re.IGNORECASE,
)


def _ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    """Render PDF pages to images and OCR them.

    Uses pypdfium2 to render at 200 DPI, applies bilateral filter + Otsu
    threshold for clean text, then runs Tesseract with PSM=3.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("pypdfium2 not installed — cannot OCR scanned PDFs")
        return ""

    all_text = []
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
        for page_idx in range(len(pdf)):
            page = pdf[page_idx]
            # Render at 200 DPI
            bitmap = page.render(scale=200 / 72)
            pil_image = bitmap.to_pil()

            # Convert to grayscale numpy array for preprocessing
            gray = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2GRAY)

            # Bilateral filter — removes noise while preserving text edges
            filtered = cv2.bilateralFilter(gray, 15, 75, 75)

            # Otsu threshold — auto-determines optimal binary threshold
            _, binary = cv2.threshold(filtered, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

            # Convert back to PIL for Tesseract
            processed = Image.fromarray(binary)

            # OCR with PSM=3 (fully automatic)
            page_text = ocr_page(processed, psm=3)
            if page_text:
                all_text.append(page_text)

        pdf.close()
    except Exception as e:
        logger.warning("OCR failed: %s", e)

    return "\n".join(all_text)


def _parse_notice_text(text: str) -> NoticeData | None:
    """Parse a single foreclosure notice from PDF text."""
    if len(text) < 100:
        return None

    notice = NoticeData(
        notice_type="foreclosure",
        county="Bell",
        state="TX",
        date_added=datetime.now().strftime("%Y-%m-%d"),
    )

    # Extract address
    addr_match = _ADDR_RE.search(text)
    if addr_match:
        notice.address = addr_match.group(1).strip().title()
        notice.city = addr_match.group(2).strip().title()
        if addr_match.group(3):
            notice.zip = addr_match.group(3)

    # Extract owner. If the "executed by" pattern picks up a known TX
    # substitute trustee (filing attorney, not the actual borrower) leave
    # owner_name blank so downstream CAD lookup can fill the real owner.
    owner_match = _OWNER_RE.search(text)
    if owner_match:
        name = owner_match.group(1).strip()
        if name.isupper():
            name = name.title()
        from scrapers.trustee_blocklist import is_substitute_trustee
        if is_substitute_trustee(name):
            logger.info(
                "Skipped substitute trustee %r on Bell foreclosure; "
                "leaving owner_name blank for CAD lookup", name,
            )
        else:
            notice.tax_owner_name = name  # PAPERTRAIL: preserve raw
            notice.owner_name = name

    # Extract sale date
    sale_match = _SALE_DATE_RE.search(text)
    if sale_match:
        raw = sale_match.group(1).strip()
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                notice.auction_date = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    notice.raw_text = text[:1000]

    if notice.owner_name or notice.address:
        return notice
    return None


@register("Bell", "foreclosure")
class BellForeclosureScraper:
    """Scrape foreclosure notices from Bell County Clerk monthly PDFs."""

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        logger.info("Bell foreclosure scrape: mode=%s", mode)

        all_notices: list[NoticeData] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(30000)

            try:
                await page.goto(BELL_FORECLOSURE_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                # Find all PDF links for current/upcoming months
                pdf_links = await page.evaluate("""
                    (() => {
                        var links = document.querySelectorAll('a[href$=".pdf"], a[href*=".pdf?"]');
                        var results = [];
                        links.forEach(a => {
                            var text = a.textContent.trim();
                            var href = a.href;
                            if (text.toLowerCase().includes('foreclosure')) {
                                results.push({text: text, href: href});
                            }
                        });
                        return results;
                    })()
                """)

                if not pdf_links:
                    logger.info("No foreclosure PDF links found on Bell County page")
                    await browser.close()
                    return []

                # Filter to recent months based on mode
                now = datetime.now()
                current_month = now.strftime("%B")
                next_month = (now.replace(day=1) + timedelta(days=32)).strftime("%B")

                if mode == "historical":
                    target_pdfs = pdf_links  # All available
                else:
                    # Current and next month only
                    target_pdfs = [
                        l for l in pdf_links
                        if current_month.lower() in l["text"].lower()
                        or next_month.lower() in l["text"].lower()
                    ]

                logger.info("Found %d foreclosure PDFs to process", len(target_pdfs))

                for pdf_info in target_pdfs:
                    if max_notices and len(all_notices) >= max_notices:
                        break

                    href = pdf_info["href"]
                    label = pdf_info["text"]
                    logger.info("  Downloading: %s", label)

                    try:
                        # Download PDF via Playwright's request context
                        resp = await context.request.get(href)
                        if resp.status != 200:
                            logger.warning("  Failed to download %s: HTTP %d", label, resp.status)
                            continue

                        pdf_bytes = await resp.body()
                        if len(pdf_bytes) < 100:
                            logger.warning("  PDF too small: %d bytes", len(pdf_bytes))
                            continue

                        # Extract text — try text layer first, fall back to OCR
                        text = extract_text(io.BytesIO(pdf_bytes))
                        if not text or len(text) < 50:
                            logger.info("  No text layer — running OCR...")
                            text = _ocr_pdf_bytes(pdf_bytes)
                            if not text or len(text) < 50:
                                logger.warning("  OCR also returned no text — skipping")
                                continue
                            logger.info("  OCR extracted %d chars", len(text))

                        # Split into individual notices if multiple in one PDF
                        # Notices typically separated by "NOTICE OF" headers
                        notice_texts = re.split(
                            r"(?=NOTICE\s+OF\s+(?:SUBSTITUTE\s+)?TRUSTEE)",
                            text,
                            flags=re.IGNORECASE,
                        )

                        for nt in notice_texts:
                            if len(nt) < 100:
                                continue
                            notice = _parse_notice_text(nt)
                            if notice:
                                notice.source_url = href
                                all_notices.append(notice)

                        logger.info("  Extracted %d notices from %s", len(notice_texts) - 1, label)

                    except Exception as e:
                        logger.warning("  Error processing %s: %s", label, e)
                        continue

            except Exception as e:
                logger.error("Bell foreclosure scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        logger.info(
            "Bell foreclosure scrape complete: %d notices",
            len(all_notices),
        )
        return all_notices

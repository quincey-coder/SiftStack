"""Reusable reCAPTCHA v2 solver for Playwright pages via 2Captcha.

Used by Odyssey Portal scrapers (Travis + Bell counties) which require
reCAPTCHA v2 on their Smart Search page.

Usage:
    from scrapers.captcha import solve_recaptcha
    success = await solve_recaptcha(page, sitekey="...", api_key="...")
"""

import logging

from playwright.async_api import Page

logger = logging.getLogger(__name__)


async def solve_recaptcha(
    page: Page,
    sitekey: str,
    api_key: str,
    max_retries: int = 3,
) -> bool:
    """Solve reCAPTCHA v2 on a Playwright page using 2Captcha.

    Args:
        page: Playwright page with the reCAPTCHA iframe
        sitekey: The reCAPTCHA sitekey (from data-sitekey attribute)
        api_key: 2Captcha API key
        max_retries: Number of retry attempts

    Returns:
        True if CAPTCHA was solved successfully, False otherwise.
    """
    if not api_key:
        logger.error("CAPTCHA_API_KEY not set — cannot solve reCAPTCHA")
        return False

    from twocaptcha import TwoCaptcha

    solver = TwoCaptcha(api_key)
    page_url = page.url

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Solving reCAPTCHA (attempt %d/%d)...", attempt, max_retries)
            result = solver.recaptcha(sitekey=sitekey, url=page_url)
            token = result.get("code", "")

            if not token:
                logger.warning("2Captcha returned empty token (attempt %d)", attempt)
                continue

            # Inject the token into the page
            await page.evaluate(f"""
                document.getElementById('g-recaptcha-response').value = '{token}';
                // Also set in any nested iframes
                var textareas = document.querySelectorAll('textarea[id="g-recaptcha-response"]');
                textareas.forEach(ta => ta.value = '{token}');
            """)

            logger.info("reCAPTCHA solved successfully")
            return True

        except Exception as e:
            logger.warning("2Captcha attempt %d failed: %s", attempt, e)
            if attempt == max_retries:
                logger.error("All %d CAPTCHA attempts failed", max_retries)
                return False

    return False


async def detect_sitekey(page: Page) -> str | None:
    """Extract reCAPTCHA sitekey from the page.

    Looks for data-sitekey attribute on reCAPTCHA div or iframe.
    Returns the sitekey string or None if not found.
    """
    sitekey = await page.evaluate("""
        (() => {
            // Check for data-sitekey on div
            var div = document.querySelector('.g-recaptcha[data-sitekey]');
            if (div) return div.getAttribute('data-sitekey');

            // Check in iframe src
            var iframe = document.querySelector('iframe[src*="recaptcha"]');
            if (iframe) {
                var src = iframe.getAttribute('src') || '';
                var match = src.match(/[?&]k=([^&]+)/);
                if (match) return match[1];
            }

            return null;
        })()
    """)

    if sitekey:
        logger.debug("Detected reCAPTCHA sitekey: %s", sitekey[:20] + "...")
    return sitekey

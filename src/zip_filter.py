"""Investor zip code filter — only keep records in target zip codes.

Loads qualifying zip codes from target_zips.json and filters records
to only include properties in those investor-active areas.

Target zips are determined by DataSift Market Finder analysis:
  - 10+ investor transactions per month threshold
  - 34 qualifying zips across Travis, Bell, Williamson counties

Edit target_zips.json to add/remove zip codes. The "custom" array
lets you add special zips that don't meet the threshold but you
want to include anyway.
"""

import json
import logging
from pathlib import Path

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# Default path — sits alongside other src/ modules
_DEFAULT_PATH = Path(__file__).parent / "target_zips.json"


def load_target_zips(path: Path | None = None) -> set[str]:
    """Load target zip codes from JSON file.

    Returns a flat set of all qualifying zips (all counties + custom combined).
    Returns empty set if file doesn't exist (no filtering applied).
    """
    filepath = path or _DEFAULT_PATH

    if not filepath.exists():
        logger.warning("target_zips.json not found at %s — zip filtering disabled", filepath)
        return set()

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read target_zips.json: %s", e)
        return set()

    all_zips: set[str] = set()

    # Collect from all county arrays + custom
    for key, value in data.items():
        if isinstance(value, list):
            for z in value:
                z_str = str(z).strip()[:5]
                if z_str and z_str.isdigit():
                    all_zips.add(z_str)

    logger.info("Loaded %d target zip codes from %s", len(all_zips), filepath.name)
    return all_zips


def filter_by_target_zips(
    notices: list[NoticeData],
    target_zips: set[str],
) -> list[NoticeData]:
    """Filter notices to only include records in target zip codes.

    Records with NO zip code pass through (haven't been geocoded yet).
    Records with a zip NOT in the target set are removed.

    Returns filtered list.
    """
    if not target_zips:
        return notices  # No filter configured

    before = len(notices)
    result = []
    removed = 0

    for n in notices:
        zip_code = n.zip.strip()[:5] if n.zip else ""
        if not zip_code:
            # No zip yet — keep it (will be filtered after Smarty/CAD fills zip)
            result.append(n)
        elif zip_code in target_zips:
            result.append(n)
        else:
            removed += 1

    if removed:
        logger.info(
            "  Removed %d records in non-target zip codes (%d kept, %d no-zip passed through)",
            removed, sum(1 for n in result if n.zip), sum(1 for n in result if not n.zip),
        )

    return result

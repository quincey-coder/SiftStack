"""Shared substitute-trustee blocklist used by all 3 TX foreclosure scrapers.

When a foreclosure notice extracts a name that matches one of these — the
filing attorney/trustee, not the actual borrower — the scraper should leave
``owner_name`` blank so downstream CAD address lookup can fill the real
property owner from the tax roll.

Add new names as they're observed (the same person typically files dozens
of foreclosures across the year).
"""
import re

# Lowercase forms; both ``LAST FIRST`` and ``FIRST LAST`` orderings included
# so the check works against either grid format.
_SUBSTITUTE_TRUSTEES = frozenset({
    "zavala angela", "angela zavala",
    "saucedo israel", "israel saucedo",
    "tabor grant", "grant tabor",
    "arnold patrice", "patrice arnold",
    "jones paige", "paige jones",
    "oliver maisyn", "maisyn oliver",
    "koponen darrick", "darrick koponen",
})


def is_substitute_trustee(name: str) -> bool:
    """Return True if ``name`` is a known TX substitute trustee."""
    if not name:
        return False
    n = re.sub(r"[^\w\s]", "", name).lower().strip()
    n = re.sub(r"\s+", " ", n)
    return n in _SUBSTITUTE_TRUSTEES

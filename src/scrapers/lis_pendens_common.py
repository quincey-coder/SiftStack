"""Pick the DEFENDANT (property owner being sued) — our lead — from a lis
pendens record's two indexed parties.

A lis pendens (Tex. Prop. Code § 12.007) is a recorded notice of a pending suit
affecting title to real property. The distressed lead is the DEFENDANT — the
property owner being sued — NOT the plaintiff who filed. Plaintiffs on a lis
pendens are commonly HOAs / property-owners associations (assessment-lien
foreclosure), lenders (judicial / home-equity foreclosure), taxing units, or the
law firms filing on their behalf. But in divorce / partition / heirship suits
both parties are individuals who own (or claim) the property, so either is a
usable lead — and the county-clerk default is fine there.

Like liens, county-clerk indexing puts the filing party on EITHER the grantor
[R] or grantee [E] side, so we pick the party that is NOT the institutional
plaintiff. Reuses `lien_common.is_creditor` (banks / IRS / debt-buyers /
hospitals) and layers on lis-pendens-specific plaintiff indicators (HOAs,
associations, law firms). Kept deliberately precise so a legitimate investor LLC
owner being sued isn't misread as the plaintiff.
"""

import re

from scrapers.lien_common import is_creditor

# Phrase indicators for a lis-pendens PLAINTIFF/filer. Multi-word so a defendant
# surname can't accidentally match ("CONDOMINIUM ASSOCIATION", not bare
# "CONDOMINIUM"; "LAW OFFICE", not bare "LAW").
_PLAINTIFF_KEYWORDS = [
    "OWNERS ASSOCIATION", "OWNER'S ASSOCIATION", "HOMEOWNERS ASSOCIATION",
    "HOME OWNERS ASSOCIATION", "PROPERTY OWNERS ASSOCIATION",
    "CONDOMINIUM ASSOCIATION", "COMMUNITY ASSOCIATION", "MASTER ASSOCIATION",
    "MAINTENANCE ASSOCIATION", "NEIGHBORHOOD ASSOCIATION",
    # Law firms filing suit on behalf of a plaintiff
    "LAW FIRM", "LAW OFFICE", "LAW OFFICES", "LAW GROUP", "ATTORNEY AT LAW",
    # Governmental / taxing-unit plaintiffs (judicial tax-foreclosure suits).
    # Live Bell 2026-07-22: the clerk indexed "TAX APPRAISAL DISTRICT OF BELL
    # COUNTY" as the GRANTEE on a batch of tax-suit lis pendens, so the default
    # grantee-is-defendant rule made the taxing district the lead — these
    # keywords force it to the plaintiff side. "CITY OF"/"COUNTY OF" also match
    # clerk-inverted forms like "GEORGETOWN CITY OF".
    "APPRAISAL DISTRICT", "CITY OF", "COUNTY OF", "SCHOOL DISTRICT",
    "MUNICIPAL UTILITY DISTRICT", "WATER DISTRICT", "TAXING UNIT",
    # TX delinquent-tax collection firms that file suit for taxing units
    "LINEBARGER", "MCCREARY", "PERDUE BRANDON",
]
_PLAINTIFF_RE = re.compile("|".join(re.escape(k) for k in _PLAINTIFF_KEYWORDS))

# Short all-caps plaintiff tokens matched whole-word (substring would be unsafe).
_PLAINTIFF_TOKENS = {"HOA", "POA", "PLLC", "LLP", "PC", "ISD", "MUD"}

# Clerk "also known as" marker left dangling at the end of an indexed party
# name ("WALKER VERDIE AKA" — the alias itself is on a separate index row).
# Stripped so it can't pollute owner_name / CAD name search.
_TRAILING_AKA_RE = re.compile(r"[\s,]+A[./]?K[./]?A\.?$", re.IGNORECASE)


def is_plaintiff(name: str) -> bool:
    """True if the party looks like a lis-pendens plaintiff/filer.

    Any institutional creditor (via ``lien_common.is_creditor``) plus HOAs,
    property-owners associations, and law firms.
    """
    if not name:
        return False
    up = name.upper()
    if is_creditor(up):
        return True
    if _PLAINTIFF_RE.search(up):
        return True
    tokens = set(re.split(r"[^A-Z0-9]+", up))
    return bool(tokens & _PLAINTIFF_TOKENS)


# Clerk placeholder party names — not a person, never a lead. Seen live:
# "GRANTEE UNKNOWN" on probate/heirship lis pendens (2026-07-13, Travis).
_PLACEHOLDER_RE = re.compile(r"\bUNKNOWN\b", re.IGNORECASE)


def pick_defendant(grantor: str, grantee: str) -> tuple[str, str]:
    """Return (defendant, plaintiff) from the two parties on a lis pendens record.

    The plaintiff is the institutional / filing party; the defendant (our lead)
    is the other. When neither or both look institutional, default to
    grantee = defendant — the common county-clerk convention where the [R]
    filer records the notice against the [E] property owner.
    """
    grantor = _TRAILING_AKA_RE.sub("", (grantor or "").strip()).strip()
    grantee = _TRAILING_AKA_RE.sub("", (grantee or "").strip()).strip()
    # Clerk placeholders ("GRANTEE UNKNOWN") are not marketable parties — blank
    # them so a placeholder can never become the lead (a blank defendant makes
    # the caller skip the record).
    if _PLACEHOLDER_RE.search(grantor):
        grantor = ""
    if _PLACEHOLDER_RE.search(grantee):
        grantee = ""
    g_pl = is_plaintiff(grantor)
    e_pl = is_plaintiff(grantee)
    if e_pl and not g_pl:
        return grantor, grantee   # grantee is the plaintiff → grantor is the defendant
    if g_pl and not e_pl:
        return grantee, grantor   # grantor is the plaintiff → grantee is the defendant
    return grantee, grantor       # default: grantee = defendant, grantor = plaintiff

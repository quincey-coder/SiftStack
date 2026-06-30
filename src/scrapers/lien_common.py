"""Shared lien helpers — pick the DEBTOR (our lead) from the two record parties.

County-clerk indexing of liens is inconsistent: on some records the individual
debtor is the grantor, on others the grantee — the institutional creditor /
debt-buyer takes the opposite role. Verified live 2026-06-29:
  Travis tccsearch:  [R] CITIBANK / [E] MOLINA RAMON G   → debtor = grantee
  Bell publicsearch: GRANTOR Vowiell Ryan / GRANTEE MIDLAND CREDIT MGMT INC
                                                        → debtor = grantor
For REI we want the distressed PROPERTY OWNER (the debtor), so we pick the party
that is NOT an institutional creditor, regardless of which column it's in.
"""

import re

# High-precision institutional-creditor / debt-buyer / filer indicators. If a
# party matches, it is the creditor side and the OTHER party is the debtor lead.
# Kept deliberately specific so a legitimate investor LLC owner isn't misread as
# a creditor.
_CREDITOR_KEYWORDS = [
    # Debt buyers / collection agencies
    "MIDLAND", "PORTFOLIO RECOVERY", "LVNV", "CACH ", "CAVALRY", "CROWN ASSET",
    "JEFFERSON CAPITAL", "RESURGENT", "RECEIVABLES", "CREDIT MANAGEMENT",
    "CREDIT SERVICES", "ASSET ACCEPTANCE", "UNIFUND", "VELOCITY INVESTMENTS",
    # Banks / card issuers
    "BANK", "CITIBANK", "CAPITAL ONE", "WELLS FARGO", "DISCOVER", "SYNCHRONY",
    "AMERICAN EXPRESS", "BARCLAYS", "COMENITY", "CREDIT UNION", " FCU", " FSB",
    "JPMORGAN", "CHASE", "USAA", "NAVY FEDERAL",
    # Title / abstract / insurance / mortgage / finance
    "ABSTRACT", "TITLE CO", "TITLE COMPANY", "INSURANCE", "MORTGAGE",
    "FINANCE", "FUNDING", "ACCEPTANCE CORP", "FINANCIAL",
    # Government taxing authorities (Federal/State tax liens)
    "STATE OF TEXAS", "UNITED STATES", "COMPTROLLER", "INTERNAL REVENUE",
    "DEPARTMENT OF", "TEXAS WORKFORCE",
    # Hospitals / health systems (hospital liens — the hospital is the creditor)
    "HOSPITAL", "HEALTH", "MEDICAL", "CLINIC", "ADVENTHEALTH", "ASCENSION",
    "BAYLOR SCOTT", "SETON", "EMERGENCY", "PHYSICIANS", "SURGERY",
]

_CREDITOR_RE = re.compile("|".join(re.escape(k) for k in _CREDITOR_KEYWORDS))

# Short all-caps creditor tokens matched whole-word (substring match would be
# unsafe — "IRS" inside another word, etc.).
_CREDITOR_TOKENS = {"IRS", "USA", "TWC", "HOA"}


def is_creditor(name: str) -> bool:
    """True if the party name looks like an institutional creditor/filer."""
    if not name:
        return False
    up = name.upper()
    if _CREDITOR_RE.search(up):
        return True
    tokens = set(re.split(r"[^A-Z0-9]+", up))
    return bool(tokens & _CREDITOR_TOKENS)


def pick_debtor(grantor: str, grantee: str) -> tuple[str, str]:
    """Return (debtor, creditor) from the two parties on a lien record.

    The creditor is the institutional party; the debtor is the other one.
    When neither or both look institutional, default to grantee = debtor
    (the most common county-clerk convention).
    """
    grantor = (grantor or "").strip()
    grantee = (grantee or "").strip()
    g_cred = is_creditor(grantor)
    e_cred = is_creditor(grantee)
    if e_cred and not g_cred:
        return grantor, grantee   # grantee is the creditor → grantor is the debtor
    if g_cred and not e_cred:
        return grantee, grantor   # grantor is the creditor → grantee is the debtor
    return grantee, grantor       # default: grantee = debtor, grantor = creditor

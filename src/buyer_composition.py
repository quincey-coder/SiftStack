"""Buyer composition analysis — decompose county "investor transactions" by buyer type.

Market Finder's headline "investor transactions" count is a black box: it lumps
production homebuilders, Wall-Street iBuyers/SFR funds, mortgage lenders/relocation
services, and genuine local flippers/landlords into one number. In TX metros the
first three categories dominate (~70%+ of volume), so the raw count badly overstates
the local cash-buyer pool a wholesaler can actually assign contracts to.

This module classifies the bundled nationwide buyer dataset (county deed records,
buyer-entity level with 6-month purchase counts) so both the market-research report
and the cash-buyer list can strip builders/institutions/lenders out.

Data source: buyer-prospector.skill -> data/nationwide_buyers.csv
  Columns: FIPS, County Name, County State, BuyerFullName, BuyerState, BuyerCity,
           BuyerAddress, BuyerZIP, BuyerPurchases6MSum
  NOTE: BuyerState and BuyerCity columns are SWAPPED in the source data
        (BuyerState holds the city, BuyerCity holds the 2-letter state).
        load_rows() un-swaps them into clean `city` / `state` keys.
"""

import csv
import io
import re
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ZIP = ROOT / "Skills for REI" / "improved" / "buyer-prospector.skill"
ZIP_MEMBER = "buyer-prospector/data/nationwide_buyers.csv"
EXTRACTED_CSV = ROOT / "data" / "nationwide_buyers.csv"  # optional faster local copy

# ── Category taxonomy ─────────────────────────────────────────────────
# Genuine pool label matches the existing Bell_TX_midtier_ranked.csv convention.
CAT_BUILDER = "Builder / Developer"
CAT_INSTITUTIONAL = "iBuyer / Institutional"
CAT_LENDER = "Lender / Relocation"
CAT_GOV = "Government / Nonprofit"
CAT_LOCAL = "Local Investor/Landlord/Flipper"

# Categories that are NOT part of the local wholesale cash-buyer pool.
EXCLUDED_CATEGORIES = (CAT_BUILDER, CAT_INSTITUTIONAL, CAT_LENDER, CAT_GOV)

# Order of evaluation matters — first match wins (see classify()).
_GOV = re.compile(
    r"\b(HOUSING AUTHORITY|CITY OF|COUNTY OF|STATE OF|GOVERNMENT|HABITAT FOR HUMANITY|"
    r"HABITAT|NONPROFIT|FOUNDATION|MINISTR(Y|IES)|CHURCH|SCHOOL DISTRICT|"
    r"REDEVELOPMENT AGENCY|HOMEOWNERS ASSOC\w*|HOMEOWNERS)\b"
)
_LENDER = re.compile(
    r"\b(LENDING|MORTGAGE|LOANS?|NEWREZ|PLANET HOME|VILLAGE CAPITAL|VETERANS UNITED|"
    r"NAF CASH|RELOCATION|RELO|NOMINEE|CARTUS|SIRVA|NEI GLOBAL|GRAEBEL|FANNIE MAE|"
    r"FREDDIE MAC|FEDERAL HOME LOAN|FEDERAL NATIONAL|SECRETARY OF (HOUSING|VETERANS)|"
    r"HUD|BANK NATIONAL ASSOCIATION|SERVICING|SERVBANK|FINANCIAL FREEDOM|REO|"
    r"KIAVI|BGRS|LIMA ONE|VISIO|COREVEST|ANCHOR LOANS)\b"
)
_INSTITUTIONAL = re.compile(
    r"\b(OPENDOOR|OFFERPAD|ZILLOW|INVITATION HOMES|AMERICAN HOMES 4 RENT|AMH |AH4R|"
    r"PROGRESS RESIDENTIAL|TRICON|FIRSTKEY|FIRST KEY|VINEBROOK|MAYMONT|SFR\d?|SPV|DIVVY|"
    r"PATHLIGHT|CERBERUS|CONREX|MAIN STREET RENEWAL|HOME PARTNERS|AMERICAN RESIDENTIAL|"
    r"ROOFSTOCK|RESICAP|STARWOOD|BLACKSTONE|AMHERST|MILLROSE|ACQUISITION TRUST|"
    r"PROPERTIES FUNDING|PROPCO|BORROWER|RCAF|RCF \d|PR ARC)\b"
)
_BUILDER = re.compile(
    r"\b(BUILDER|BUILDERS|HOMEBUILD\w*|CONSTRUCTION|DEVELOPMENT|DEVELOPERS?|"
    r"D ?R HORTON|DR HORTON|LENNAR|PULTE\w*|KB HOME|MERITAGE|TAYLOR MORRISON|"
    r"HIGHLAND HOMES|PERRY HOMES|BROHN|MILESTONE COMMUNIT\w+|CENTURY COMMUNITIES|"
    r"CASTLEROCK|GEHAN|LGI HOMES?|DREES|DAVID WEEKLEY|WEEKLEY HOMES|COVENTRY HOMES|"
    r"M ?I HOMES|ASHTON WOODS|ASHTON AUSTIN|CHESMAR|PACESETTER|NEWMARK HOMES|"
    r"EMPIRE COMMUNITIES|SCOTT FELDER|CALATLANTIC|SITTERLE|GFO HOME|TRENDMAKER|"
    r"HISTORYMAKER|RAUSCH COLEMAN|DSLD|STYLECRAFT|TERRATA|CONTINENTAL HOMES|"
    r"CLAYTON|WESTIN HOMES|STARLIGHT HOMES|MCGUYER|PLANTATION HOMES|"
    r"BROOKFIELD RESIDENTIAL|SARATOGA HOMES|HOMES OF TEXAS)\b"
)

# Entity-type buckets (matches buyer-prospector analyze_buyers.py convention).
_BUSINESS_KW = ("PROPERTIES", "HOLDINGS", "INVESTMENTS", "CAPITAL", "GROUP", "PARTNERS",
                "COMPANY", "ENTERPRISES", "VENTURES", "REALTY", "HOMES", "REAL ESTATE",
                "BUILDERS", "CONSTRUCTION", "MANAGEMENT", "SOLUTIONS", "SERVICES",
                "ASSOCIATES", "FUNDING", "ACQUISITIONS", "BUYERS", "RENOVATIONS",
                "DEVELOPMENT", "CONSULTING")


def classify(name: str) -> str:
    """Classify a buyer entity name into one of the category constants."""
    if not name:
        return CAT_LOCAL
    n = name.upper()
    if _GOV.search(n):
        return CAT_GOV
    if _LENDER.search(n):
        return CAT_LENDER
    if _INSTITUTIONAL.search(n):
        return CAT_INSTITUTIONAL
    if _BUILDER.search(n):
        return CAT_BUILDER
    return CAT_LOCAL


def entity_type(name: str) -> str:
    """Classify the legal-entity wrapper (LLC / TRUST / CORP / LP / INDIVIDUAL...)."""
    if not name:
        return "UNKNOWN"
    n = name.upper().strip()
    if "LLC" in n or "L.L.C" in n:
        return "LLC"
    if "TRUST" in n or n.endswith(" TRU"):
        return "TRUST"
    if any(k in n for k in (" INC", " INC.", "INCORPORATED")):
        return "CORPORATION"
    if any(k in n for k in (" CORP", " CORP.", "CORPORATION")):
        return "CORPORATION"
    if "ESTATE" in n:
        return "ESTATE"
    if any(k in n for k in (" LP", " LLP", "LIMITED PARTNERSHIP")):
        return "LIMITED PARTNERSHIP"
    words = n.split()
    if len(words) <= 3 and not any(k in n for k in _BUSINESS_KW):
        return "INDIVIDUAL"
    return "OTHER ENTITY"


def _open_dataset():
    """Return a text stream over the nationwide buyer CSV (zip or extracted copy)."""
    if EXTRACTED_CSV.exists():
        return open(EXTRACTED_CSV, "r", encoding="utf-8", errors="replace")
    if SKILL_ZIP.exists():
        with zipfile.ZipFile(SKILL_ZIP) as zf:
            raw = zf.read(ZIP_MEMBER).decode("utf-8", "replace")
        return io.StringIO(raw)
    raise FileNotFoundError(
        f"nationwide_buyers.csv not found (looked in {EXTRACTED_CSV} and {SKILL_ZIP})"
    )


def load_rows(county: str, state: str = "TX") -> list[dict]:
    """Load and clean buyer rows for one county.

    Returns dicts with keys: entity, purchases (int), city, state, zip, address,
    category, entity_type. The swapped city/state source columns are corrected here.
    """
    cty = county.strip().upper().replace(" COUNTY", "")
    st = state.strip().upper()
    rows = []
    stream = _open_dataset()
    try:
        for r in csv.DictReader(stream):
            if (r.get("County State") or "").strip().upper() != st:
                continue
            if (r.get("County Name") or "").strip().upper() != cty:
                continue
            try:
                purchases = int(float(r.get("BuyerPurchases6MSum") or 0))
            except ValueError:
                purchases = 0
            name = (r.get("BuyerFullName") or "").strip()
            rows.append({
                "entity": name,
                "purchases": purchases,
                # source columns are swapped: "BuyerState" holds city, "BuyerCity" holds state
                "city": (r.get("BuyerState") or "").strip(),
                "state": (r.get("BuyerCity") or "").strip(),
                "zip": (r.get("BuyerZIP") or "").strip(),
                "address": (r.get("BuyerAddress") or "").strip(),
                "category": classify(name),
                "entity_type": entity_type(name),
            })
    finally:
        if hasattr(stream, "close"):
            stream.close()
    rows.sort(key=lambda x: x["purchases"], reverse=True)
    return rows


def analyze_county(county: str, state: str = "TX") -> dict:
    """Return a composition summary + classified rows for one county.

    Keys: county, state, rows, total_entities, total_purchases,
          by_category {cat: {"entities", "purchases", "pct"}},
          local_rows, excluded_skew_pct, local_pct, top_local, top_excluded.
    """
    rows = load_rows(county, state)
    total_p = sum(r["purchases"] for r in rows) or 1
    by_cat = defaultdict(lambda: {"entities": 0, "purchases": 0})
    for r in rows:
        c = by_cat[r["category"]]
        c["entities"] += 1
        c["purchases"] += r["purchases"]
    for c in by_cat.values():
        c["pct"] = round(c["purchases"] / total_p * 100, 1)

    local_rows = [r for r in rows if r["category"] == CAT_LOCAL]
    excluded_rows = [r for r in rows if r["category"] in EXCLUDED_CATEGORIES]
    excluded_p = sum(r["purchases"] for r in excluded_rows)

    return {
        "county": county.title(),
        "state": state.upper(),
        "rows": rows,
        "total_entities": len(rows),
        "total_purchases": sum(r["purchases"] for r in rows),
        "by_category": dict(by_cat),
        "local_rows": local_rows,
        "excluded_skew_pct": round(excluded_p / total_p * 100, 1),
        "local_pct": round(sum(r["purchases"] for r in local_rows) / total_p * 100, 1),
        "top_local": local_rows[:15],
        "top_excluded": sorted(excluded_rows, key=lambda x: x["purchases"], reverse=True)[:10],
    }

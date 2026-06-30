"""Build a CLEAN cash-buyer list for a TX county — genuine local investors only.

Strips production builders, iBuyers/SFR funds, mortgage lenders/relocation services,
and government/HOA entities out of the bundled nationwide buyer dataset, leaving the
local flipper/landlord pool a wholesaler can actually assign contracts to.

Usage:
    python scripts/build_buyer_list.py Travis
    python scripts/build_buyer_list.py Bell

Outputs (in output/):
    <County>_TX_buyers_CLEAN.csv        — ranked genuine local investors
    <County>_TX_Cash_Buyers_SIFT_UPLOAD.csv — DataSift-ready upload (no DM research yet)

NOTE: This is the FIRST step. Identifying the decision-maker behind each LLC
(the FOUND/NOTFOUND master lists) is a separate enrichment that needs SOS/Bizapedia
lookups — see the buyer-prospector skill and project_bell_buyers_master memory.
"""

import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUTPUT_DIR = ROOT / "output"

import buyer_composition as bc  # noqa: E402


def build(county: str, state: str = "TX") -> None:
    a = bc.analyze_county(county, state)
    local = a["local_rows"]
    today = datetime.now().strftime("%-m/%-d/%Y")

    # ── 1. Clean ranked list ───────────────────────────────────────────
    clean_path = OUTPUT_DIR / f"{a['county']}_{state}_buyers_CLEAN.csv"
    with open(clean_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Entity", "Category", "EntityType", "Purchases_6mo",
                    "Txns_per_Month", "MidTier_2_25", "City", "State", "ZIP", "Address"])
        for i, r in enumerate(local, 1):
            w.writerow([i, r["entity"], r["category"], r["entity_type"], r["purchases"],
                        round(r["purchases"] / 6, 1), 2 <= r["purchases"] <= 25,
                        r["city"], r["state"], r["zip"], r["address"]])

    # ── 2. DataSift-ready SIFT upload (mirrors Bell_TX_Cash_Buyers_SIFT_UPLOAD) ──
    sift_path = OUTPUT_DIR / f"{a['county']}_{state}_Cash_Buyers_SIFT_UPLOAD.csv"
    with open(sift_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Property Street Address", "Property City", "Property State",
                    "Property ZIP Code", "Owner First Name", "Owner Last Name",
                    "Mailing Street Address", "Mailing City", "Mailing State",
                    "Mailing ZIP Code", "Tags", "Lists", "Notes", "Entity Type",
                    "Entity Contact", "Entity Contact Role", "County", "Date Added",
                    "Source URL"])
        for r in local:
            tags = (f"Cash Buyer, Buyers List, {a['county']} {state}, Courthouse Data, "
                    f"{r['entity_type']}")
            notes = (f"Active buyer: {r['purchases']} purchases in 6mo. "
                     f"Category: {r['category']}. DM research pending.")
            w.writerow([
                "", "", "", "",                       # property address (buyers, not props)
                "", r["entity"],                      # owner name = entity (no DM yet)
                r["address"], r["city"], r["state"], r["zip"],   # mailing = buyer address
                tags, f"{a['county']} County Cash Buyers", notes,
                r["entity_type"], "", "",             # Entity Contact / Role — DM pending
                a["county"], today,
                "nationwide_buyers.csv (county deed records, 6-mo investor purchases)",
            ])

    # ── Console summary ────────────────────────────────────────────────
    print(f"\n{'='*64}\n  {a['county'].upper()} COUNTY, {state} — CLEAN CASH-BUYER LIST\n{'='*64}")
    print(f"  Source pool: {a['total_entities']} buyer entities / "
          f"{a['total_purchases']:,} purchases (6mo)")
    print(f"  Excluded as skew (builder/iBuyer/lender/gov): {a['excluded_skew_pct']}%")
    print(f"  GENUINE LOCAL pool kept: {len(local)} entities, {a['local_pct']}% of volume")
    midtier = [r for r in local if 2 <= r["purchases"] <= 25]
    print(f"  Mid-tier band (2-25 buys, DM-research targets): {len(midtier)} entities")
    print(f"\n  Top 10 genuine local buyers:")
    for i, r in enumerate(local[:10], 1):
        print(f"    {i:>2}. {r['entity'][:38]:<39}{r['purchases']:>4}  {r['city']}, {r['state']}")
    print(f"\n  Wrote:\n    {clean_path}\n    {sift_path}\n")


def main():
    county = sys.argv[1] if len(sys.argv) > 1 else "Travis"
    state = sys.argv[2] if len(sys.argv) > 2 else "TX"
    build(county, state)


if __name__ == "__main__":
    main()

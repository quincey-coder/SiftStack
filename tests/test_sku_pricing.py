"""SKU-grounded rehab engine tests: the locked master material list drives
Austin MATERIAL dollars, labor and every non-Austin path stay on the legacy
tables, and locked prices never take the regional multiplier (the
double-discount trap).

Run: python tests/test_sku_pricing.py   (or pytest tests/test_sku_pricing.py)

The basket tests need data/master_materials_locked_78704.json, written by
`python src/material_list.py --master --zip 78704 --lock`. Until that capture
runs they SKIP (loudly) rather than fail — the fallback and opt-out tests,
which prove a missing list degrades to the engine tables safely, always run.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sku_pricing
from rehab_estimator import (
    FLOORING_LABOR_PER_SQFT, HVAC_COSTS, KITCHEN_COSTS, REGIONAL_MULTIPLIERS,
    WINDOWS_PER_UNIT, estimate_rehab,
)

LOCKED_ZIP = sku_pricing.LOCKED_ZIP
LOCK_PATH = (Path(__file__).parent.parent / "data"
             / f"master_materials_locked_{LOCKED_ZIP}.json")
HAVE_LOCK = LOCK_PATH.exists()

# Regions that read the locked list, and regions that must never touch it.
SKU_REGIONS = sku_pricing.SKU_REGIONS
ENGINE_REGIONS = ("bell", "national")
MULT = REGIONAL_MULTIPLIERS[SKU_REGIONS[0]]

SQFT, BEDS, BATHS, YEAR = 1500, 3, 2.0, 1960


class _Skip(Exception):
    pass


def _need_lock():
    if not HAVE_LOCK:
        raise _Skip(f"no locked list at {LOCK_PATH.name}")


def _lock():
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def _grade_price(lock, key, tier):
    """Independent recomputation of the tier price straight off the JSON."""
    row = next(r for r in lock["rows"] if r["key"] == key)
    grade = row["grades"].get({1: "budget", 2: "standard", 3: "upgrade"}[tier])
    if grade is None or grade.get("quote"):
        grade = row["grades"]["standard"]
    return grade["unit_price"]


def _allow_amt(lock, key):
    return next(a for a in lock["allowances"] if a["key"] == key)["amount"]


def _est(region, tier, scope="full", **kw):
    return estimate_rehab(address="T", sqft=SQFT, bedrooms=BEDS, bathrooms=BATHS,
                          year_built=YEAR, tier=tier, scope=scope, region=region, **kw)


def _room(est, category):
    return next(r for r in est.rooms if r.category == category)


def test_lock_artifact_integrity():
    _need_lock()
    lock = _lock()
    assert lock["locked"] is True and lock["zip"] == LOCKED_ZIP
    assert lock["market"] == "austin", lock["market"]
    assert len(lock["rows"]) == 88, len(lock["rows"])
    assert len(lock["allowances"]) == 12
    unpriced = [r["item"] for r in lock["rows"] if not r["grades"].get("standard")]
    assert not unpriced, f"unpriced rows in the lock: {unpriced}"
    # graded rows carry all three grade slots
    for r in lock["rows"]:
        if r["kind"] == "graded":
            assert all(r["grades"].get(g) for g in ("budget", "standard", "upgrade")), r["key"]
    # per-case rows landed as per-sqft (lvp case price / 20.1 sf)
    lvp = next(r for r in lock["rows"] if r["key"] == "lvp")
    assert lvp["per_case_sqft"] == 20.1
    assert lvp["grades"]["standard"]["unit_price"] < 15, "lvp should be per-sqft, not per-case"


def test_non_austin_and_tier4_stay_on_engine():
    """Bell is a separate Home Depot metro and tier 4 is off-list by
    definition; neither may ever read the Austin lock."""
    for region in ENGINE_REGIONS:
        for tier in (1, 2, 3, 4):
            est = _est(region, tier)
            assert est.materials_source == "engine", (region, tier)
            assert all(r.materials_source == "engine" for r in est.rooms)
    est = _est(SKU_REGIONS[0], 4)
    assert est.materials_source == "engine"
    assert all(r.materials_source == "engine" for r in est.rooms)


def test_austin_tiers_are_sku_grounded():
    _need_lock()
    for region in SKU_REGIONS:
        for tier in (1, 2, 3):
            est = _est(region, tier)
            assert f"locked_sku {LOCKED_ZIP}" in est.materials_source, (region, tier)
            sku_rooms = [r for r in est.rooms if r.materials_source == "locked_sku"]
            assert len(sku_rooms) >= 10, (region, tier, len(sku_rooms))
            # Foundation stays engine (no basket)
            assert _room(est, "Foundation/Structural").materials_source == "engine"


def test_windows_no_multiplier_leak():
    """Windows materials == locked price x count EXACTLY; labor keeps the
    engine share x multiplier. Any 0.95 leak onto materials fails this."""
    _need_lock()
    lock = _lock()
    count = max(8, SQFT // 100)
    for tier in (1, 2, 3):
        est = _est(SKU_REGIONS[0], tier)
        room = _room(est, "Windows")
        expected_mat = round(_grade_price(lock, "window", tier) * count)
        assert room.materials == expected_mat, (tier, room.materials, expected_mat)
        assert room.labor == round(WINDOWS_PER_UNIT[tier] * count * MULT * 0.4)


def test_hvac_basket_math():
    _need_lock()
    lock = _lock()
    for tier in (1, 2, 3):
        est = _est(SKU_REGIONS[0], tier)
        room = _room(est, "HVAC")
        expected = round(_grade_price(lock, "heat_pump", tier)
                         + _grade_price(lock, "thermostat", tier)
                         + _allow_amt(lock, "heat_pump_scope_kit"))
        assert room.materials == expected, (tier, room.materials, expected)
        assert room.labor == round(HVAC_COSTS[tier] * MULT * 0.6)


def test_flooring_basket_math():
    _need_lock()
    lock = _lock()
    for tier in (1, 2, 3):
        est = _est(SKU_REGIONS[0], tier)
        room = _room(est, "Flooring")
        assert room.materials == round(_grade_price(lock, "lvp", tier) * SQFT * 1.1)
        assert room.labor == round(SQFT * FLOORING_LABOR_PER_SQFT[tier] * MULT)
        assert "materials_per_sqft" in room.line_items  # post_walkthrough contract


def test_kitchen_demo_moves_to_labor():
    _need_lock()
    for tier in (1, 2, 3):
        est = _est(SKU_REGIONS[0], tier)
        room = _room(est, "Kitchen")
        tc = KITCHEN_COSTS[tier]
        assert room.labor == round(tc["labor"] * MULT) + round(tc["demo"] * MULT)
        # component-table line_items contract: rows sum to the room total
        assert abs(sum(room.line_items.values()) - room.total) <= 2
        for key in ("demo", "cabinets", "countertops", "appliances", "fixtures", "labor"):
            assert key in room.line_items, key


def test_totals_are_room_sums():
    for tier in (1, 2, 3):
        est = _est(SKU_REGIONS[0], tier)
        assert est.total_materials == round(sum(r.materials for r in est.rooms))
        assert est.total_labor == round(sum(r.labor for r in est.rooms))


def test_fallback_when_lock_missing():
    real_path = sku_pricing.DATA_PATH
    try:
        sku_pricing.DATA_PATH = Path(str(LOCK_PATH) + ".missing")
        sku_pricing.reset_cache()
        est = _est(SKU_REGIONS[0], 2)
        assert est.materials_source == "engine"
        baseline = _est(SKU_REGIONS[0], 2, use_locked_materials=False)
        assert est.total_materials == baseline.total_materials
        assert est.grand_total == baseline.grand_total
    finally:
        sku_pricing.DATA_PATH = real_path
        sku_pricing.reset_cache()


def test_wrong_zip_lock_is_rejected():
    """A Knox (or any other market's) lock file must not be honoured while
    SKU_LOCKED_ZIP says Austin — that is the double-market trap."""
    import tempfile
    real_path = sku_pricing.DATA_PATH
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump({"locked": True, "zip": "37914", "market": "knox",
                       "rows": [], "allowances": []}, fh)
            tmp = fh.name
        sku_pricing.DATA_PATH = Path(tmp)
        sku_pricing.reset_cache()
        assert sku_pricing.load_locked() is None
        assert _est(SKU_REGIONS[0], 2).materials_source == "engine"
    finally:
        sku_pricing.DATA_PATH = real_path
        sku_pricing.reset_cache()
        os.unlink(tmp)


def test_opt_out_flag():
    est = _est(SKU_REGIONS[0], 2, use_locked_materials=False)
    assert est.materials_source == "engine"
    assert all(r.materials_source == "engine" for r in est.rooms)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = skipped = 0
    for fn in fns:
        try:
            fn()
        except _Skip as exc:
            print(f"SKIP {fn.__name__} ({exc})")
            skipped += 1
        else:
            print(f"PASS {fn.__name__}")
            passed += 1
    print(f"\n{passed} passed, {skipped} skipped")
    if skipped:
        print(f"Capture the list to enable them: "
              f"python src/material_list.py --master --zip {LOCKED_ZIP} --lock")

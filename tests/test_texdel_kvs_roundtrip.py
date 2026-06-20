"""Gate A — in-memory cross-run KVS round-trip test for Bell + Williamson
tax-delinquent state.

Proves the contract the Apify wiring in main.py depends on:
  inject_apify_state(county, prior)  ← what main.py does after kvs.get_value(key)
    → scraper: load_state → compute_diff → commit_run
  snapshot_apify_state(county)       → what main.py persists via kvs.set_value(key)

Two simulated processes per county (the module-level cache is cleared between
them, exactly like a fresh Apify run). Run 1 must report is_first_run=True and
seed the baseline; run 2 must load that baseline back and report a real
NEW/DROPPED diff with is_first_run=False. No network, no cloud — runs in
milliseconds.

Run: PYTHONPATH=src python tests/test_texdel_kvs_roundtrip.py
"""

import os
import sys
from pathlib import Path

# Force the Apify code path so we exercise the in-memory cache (not disk).
os.environ["APIFY_IS_AT_HOME"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import tax_delinquent_state as st  # noqa: E402

# Realistic APN tokens (must match the module's format regex ^[A-Za-z]?\d{4,12}$
# so the guardrail doesn't trip and refuse to update the baseline).
APNS_RUN1 = {"100001", "100002", "100003"}
APNS_RUN2 = {"100002", "100003", "100004"}  # +100004 (new), -100001 (dropped)

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        _failures.append(label)


def simulate_run(county: str, key: str, fake_kvs: dict, current_apns: set) -> object:
    """One full simulated Apify run for a county. Returns the DiffResult."""
    # Fresh process: module-level cache starts empty.
    st._APIFY_STATE_CACHE.clear()

    # main.py startup: load from KVS → inject into cache.
    stored = fake_kvs.get(key) or st._empty_state()
    st.inject_apify_state(county, stored)

    # Scraper: load state, diff, commit.
    state = st.load_state(county)
    prev_apns = set(state.get("last_run_apns") or [])
    diff = st.compute_diff(current_apns, prev_apns)
    # Mirror the scraper's commit guard (raw_path is a virtual apify-kvs:// path
    # on Apify, never None unless archival throws).
    st.commit_run(
        county, state,
        current_apns=current_apns,
        raw_path=Path(f"apify-kvs://{county.lower()}_test.xlsx"),
        raw_sha="deadbeef",
        diff=diff,
    )

    # main.py end-of-run: snapshot cache → persist to KVS.
    fake_kvs[key] = st.snapshot_apify_state(county)
    return diff


def test_county(county: str) -> None:
    key = f"{county.lower()}_texdel_state"
    fake_kvs: dict = {}  # stands in for the cloud sift-stack-state store
    print(f"\n=== {county} ({key}) ===")

    # ── Run 1: first run seeds the baseline ──
    diff1 = simulate_run(county, key, fake_kvs, APNS_RUN1)
    check("run1 is_first_run is True", diff1.is_first_run is True)
    check("run1 not guardrail_tripped", diff1.guardrail_tripped is False)
    check(f"run1 new_count == {len(APNS_RUN1)}", diff1.new_count == len(APNS_RUN1))
    check("run1 persisted baseline to KVS", key in fake_kvs)
    check(
        "run1 KVS baseline == sorted(APNS_RUN1)",
        fake_kvs[key].get("last_run_apns") == sorted(APNS_RUN1),
    )

    # ── Run 2: fresh process loads the baseline and diffs against it ──
    diff2 = simulate_run(county, key, fake_kvs, APNS_RUN2)
    check("run2 is_first_run is False (baseline round-tripped)", diff2.is_first_run is False)
    check("run2 not guardrail_tripped", diff2.guardrail_tripped is False)
    check("run2 NEW == {100004}", set(diff2.new) == {"100004"})
    check("run2 DROPPED == {100001}", set(diff2.dropped) == {"100001"})
    check("run2 REPEAT == {100002, 100003}", set(diff2.repeat) == {"100002", "100003"})
    check(
        "run2 KVS baseline updated to sorted(APNS_RUN2)",
        fake_kvs[key].get("last_run_apns") == sorted(APNS_RUN2),
    )


def main() -> int:
    print("Gate A: Bell + Williamson tax-delinquent KVS round-trip (in-memory)")
    for county in ("Bell", "Williamson"):
        test_county(county)

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED — {_failures}")
        return 1
    print("RESULT: ALL PASSED — KVS round-trip contract holds for Bell + Williamson.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Seed Apify's cross-run KVS with local state before the Actor's first run.

Run this ONCE after `apify push` so the deployed Actor's first scheduled run
picks up where the local CLI left off — same `seen_notice_ids`, same
`last_run_date`, same Travis tax-delinquent APN baseline for the diff, and the
Travis code-enforcement open-case baseline for resolution tracking.

What gets seeded (under the named KVS `sift-stack-state`):
  - `last_run_date`     ← last_run.json
  - `seen_notice_ids`   ← seen_ids.json (sorted list of dedup keys)
  - `travis_texdel_state`     ← data/travis_tax_state/tax_delinquent_travis_state.json
  - `bell_texdel_state`       ← data/bell_tax_state/tax_delinquent_bell_state.json (if present)
  - `williamson_texdel_state` ← data/williamson_tax_state/tax_delinquent_williamson_state.json (if present)
  - `travis_codeenf_state`    ← current OPEN Austin code cases (live fetch by default)

Code-enforcement seeding is STATE-ONLY and has NO CRM side effects. It writes
just the open-case baseline (`last_run_case_ids` / `master_case_ids`) so the
first cloud run reads currently-open cases as REPEAT (not NEW) — nothing is
scraped, enriched, uploaded, or sent to DataSift. `last_run_records` is left
empty (carried over from any existing cloud state), so a seeded case that later
resolves produces ZERO CRM cleanup rows — it was never in the CRM. Only cases
the normal daily pipeline uploads later get into `last_run_records`.

Merge-safe: existing cloud state is read first; `master_case_ids` unions, and
seeding never wipes real CRM-tracking the cloud already had.

Auth: reads APIFY_TOKEN from .env (via python-dotenv).

Usage:
    PYTHONPATH=src python scripts/seed_apify_kvs.py
    PYTHONPATH=src python scripts/seed_apify_kvs.py --dry-run          # preview sizes
    PYTHONPATH=src python scripts/seed_apify_kvs.py --codeenf-only     # ONLY seed code-enforcement state
    PYTHONPATH=src python scripts/seed_apify_kvs.py --no-codeenf       # skip code-enforcement
    PYTHONPATH=src python scripts/seed_apify_kvs.py --codeenf-source local  # use local state file, not a live fetch
    PYTHONPATH=src python scripts/seed_apify_kvs.py --kvs-name foo     # custom store name
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from apify_client import ApifyClient
except ImportError:
    print("ERROR: apify-client not installed. Run: pip install apify-client", file=sys.stderr)
    sys.exit(1)


DEFAULT_KVS_NAME = "sift-stack-state"
CODEENF_KVS_KEY = "travis_codeenf_state"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEEN_IDS_PATH = PROJECT_ROOT / "seen_ids.json"
LAST_RUN_PATH = PROJECT_ROOT / "last_run.json"
TEXDEL_STATE_PATH = PROJECT_ROOT / "data" / "travis_tax_state" / "tax_delinquent_travis_state.json"
CODEENF_STATE_PATH = PROJECT_ROOT / "data" / "travis_codeenf_state" / "code_enforcement_travis_state.json"

# Bell + Williamson share the generic county-parameterized state module; their
# local state lives under data/{county}_tax_state/. Optional — skipped if absent.
GENERIC_TEXDEL = (
    ("bell_texdel_state", PROJECT_ROOT / "data" / "bell_tax_state" / "tax_delinquent_bell_state.json"),
    ("williamson_texdel_state", PROJECT_ROOT / "data" / "williamson_tax_state" / "tax_delinquent_williamson_state.json"),
)


def _load_json_or_exit(path: Path) -> object:
    if not path.exists():
        print(f"ERROR: {path} does not exist — can't seed.", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def _load_json_optional(path: Path) -> dict | None:
    """Load a state dict if it exists; return None (skip) if it doesn't."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"WARN: {path} is not valid JSON ({e}) — skipping.", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"WARN: {path} is not a dict — skipping.", file=sys.stderr)
        return None
    return data


def _codeenf_open_ids(source: str) -> set[str]:
    """Get the current OPEN Austin code-case ids for the baseline seed.

    source="live": fetch straight from the Austin Socrata API (cheap — case_id
    only, ~4 pages). source="local": read last_run_case_ids from the local
    code-enforcement state file (must exist). Neither scrapes leads, enriches,
    uploads, or touches DataSift.
    """
    if source == "local":
        state = _load_json_optional(CODEENF_STATE_PATH)
        if not state:
            print(
                f"ERROR: --codeenf-source local but {CODEENF_STATE_PATH} is missing.",
                file=sys.stderr,
            )
            sys.exit(1)
        return set(state.get("last_run_case_ids") or [])
    # live
    from scrapers.code_enforcement_travis import TravisCodeEnforcementScraper
    return TravisCodeEnforcementScraper()._fetch_all_open_case_ids()


def _seed_codeenf(kvs, source: str, dry_run: bool) -> None:
    """Seed travis_codeenf_state (open-case baseline only, merge-safe, no CRM)."""
    from scrapers.code_enforcement_state import build_seed_state

    print("\nCode-enforcement (Travis) — building open-case baseline "
          f"(source={source})...")
    open_ids = _codeenf_open_ids(source)
    print(f"  current open cases: {len(open_ids):,}")

    # Read existing cloud state so we merge (never clobber a prior baseline /
    # real last_run_records) rather than overwrite.
    prev = None
    if kvs is not None:
        try:
            rec = kvs.get_record(CODEENF_KVS_KEY)
            prev = rec.get("value") if isinstance(rec, dict) else None
        except Exception as e:
            print(f"  (could not read existing cloud state: {e})")
    if prev:
        print(
            "  existing cloud state: last_run_case_ids="
            f"{len(prev.get('last_run_case_ids') or []):,}, "
            f"last_run_records={len(prev.get('last_run_records') or {}):,} (preserved)"
        )

    seed = build_seed_state(open_ids, prev_state=prev)
    print(
        f"  → seed: last_run_case_ids={len(seed['last_run_case_ids']):,}, "
        f"master_case_ids={len(seed['master_case_ids']):,}, "
        f"last_run_records={len(seed['last_run_records']):,} (empty = no CRM rows)"
    )
    if dry_run:
        print("  [DRY RUN] not written.")
        return
    kvs.set_record(CODEENF_KVS_KEY, seed)
    print(f"  ✓ {CODEENF_KVS_KEY} seeded (state only — nothing sent to DataSift)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--kvs-name", default=DEFAULT_KVS_NAME,
        help=f"Named KVS to seed (default: {DEFAULT_KVS_NAME})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be seeded without writing to Apify",
    )
    parser.add_argument(
        "--codeenf-only", action="store_true",
        help="Seed ONLY travis_codeenf_state (skip seen_ids / tax-delinquent).",
    )
    parser.add_argument(
        "--no-codeenf", action="store_true",
        help="Skip code-enforcement seeding.",
    )
    parser.add_argument(
        "--codeenf-source", choices=("live", "local"), default="live",
        help="Where to read the open-case baseline (default: live Austin API).",
    )
    args = parser.parse_args()

    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        print("ERROR: APIFY_TOKEN not set. Add it to .env or export it.", file=sys.stderr)
        return 1

    seed_texdel = not args.codeenf_only
    seed_codeenf = not args.no_codeenf

    # Load local tax-delinquent / seen state (skipped in --codeenf-only mode).
    last_run_date = None
    seen_ids = []
    texdel_state = {}
    generic_states = []
    if seed_texdel:
        print("Loading local state...")
        last_run = _load_json_or_exit(LAST_RUN_PATH)
        seen_ids = _load_json_or_exit(SEEN_IDS_PATH)
        texdel_state = _load_json_or_exit(TEXDEL_STATE_PATH)
        generic_states = [
            (key, state)
            for key, path in GENERIC_TEXDEL
            if (state := _load_json_optional(path)) is not None
        ]

        last_run_date = last_run.get("last_run_date") if isinstance(last_run, dict) else None
        if not last_run_date:
            print("ERROR: last_run.json missing 'last_run_date' key", file=sys.stderr)
            return 1
        if not isinstance(seen_ids, list):
            print(f"ERROR: seen_ids.json is {type(seen_ids).__name__}, expected list", file=sys.stderr)
            return 1
        if not isinstance(texdel_state, dict):
            print("ERROR: travis_texdel state is not a dict", file=sys.stderr)
            return 1

        print(f"  last_run_date       : {last_run_date}")
        print(f"  seen_notice_ids     : {len(seen_ids):,} keys")
        print(
            f"  travis_texdel_state : last_run_apns={len(texdel_state.get('last_run_apns') or []):,}, "
            f"master_apns={len(texdel_state.get('master_apns') or []):,}"
        )
        for key, state in generic_states:
            print(
                f"  {key:<19} : last_run_apns={len(state.get('last_run_apns') or []):,}, "
                f"master_apns={len(state.get('master_apns') or []):,}"
            )
        skipped = [key for key, _ in GENERIC_TEXDEL if key not in {k for k, _ in generic_states}]
        for key in skipped:
            print(f"  {key:<19} : (no local state file — will self-seed on first cloud run)")

    # Connect + get/create the named KVS (needed to read existing state for
    # merge-safe code-enforcement seeding, and to write). Skip only for a
    # tax-del dry-run with no code-enforcement.
    kvs = None
    if not (args.dry_run and not seed_codeenf):
        print(f"\nConnecting to Apify with token ...{token[-6:]}")
        client = ApifyClient(token)
        user = client.user().get()
        if not user:
            print("ERROR: APIFY_TOKEN invalid — /user endpoint returned empty", file=sys.stderr)
            return 1
        print(f"  authenticated as: {user.get('username', '?')} (id={user.get('id', '?')})")
        print(f"Opening named KVS: {args.kvs_name}")
        kvs_info = client.key_value_stores().get_or_create(name=args.kvs_name)
        # apify-client 2.x returns a dict, 3.x a typed KeyValueStore model
        kvs_id = kvs_info["id"] if isinstance(kvs_info, dict) else kvs_info.id
        kvs = client.key_value_store(kvs_id)
        print(f"  KVS id: {kvs_id}")

    # Write tax-delinquent / seen records.
    if seed_texdel and not args.dry_run:
        print("\nSeeding tax-delinquent / seen records...")
        kvs.set_record("last_run_date", last_run_date)
        print(f"  ✓ last_run_date = {last_run_date}")
        kvs.set_record("seen_notice_ids", seen_ids)
        print(f"  ✓ seen_notice_ids ({len(seen_ids):,} keys)")
        kvs.set_record("travis_texdel_state", texdel_state)
        print(f"  ✓ travis_texdel_state (last_run_apns={len(texdel_state.get('last_run_apns') or []):,})")
        for key, state in generic_states:
            kvs.set_record(key, state)
            print(f"  ✓ {key} (last_run_apns={len(state.get('last_run_apns') or []):,})")
    elif seed_texdel and args.dry_run:
        print("\n[DRY RUN] tax-delinquent / seen records not written.")

    # Seed code-enforcement (open-case baseline only).
    if seed_codeenf:
        _seed_codeenf(kvs, args.codeenf_source, args.dry_run)

    if args.dry_run:
        print("\n[DRY RUN] No writes performed.")
    else:
        print("\nDone. The next Apify Actor run will pick up from this state.")
        print(f"Verify in Apify Console: Storage → Key-value stores → {args.kvs_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

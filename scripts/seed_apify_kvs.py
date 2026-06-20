"""Seed Apify's cross-run KVS with local state before the Actor's first run.

Run this ONCE after `apify push` so the deployed Actor's first scheduled run
picks up where the local CLI left off — same `seen_notice_ids`, same
`last_run_date`, same Travis tax-delinquent APN baseline for the diff.

What gets seeded (under the named KVS `sift-stack-state`):
  - `last_run_date`     ← last_run.json
  - `seen_notice_ids`   ← seen_ids.json (sorted list of dedup keys)
  - `travis_texdel_state`     ← data/travis_tax_state/tax_delinquent_travis_state.json
  - `bell_texdel_state`       ← data/bell_tax_state/tax_delinquent_bell_state.json (if present)
  - `williamson_texdel_state` ← data/williamson_tax_state/tax_delinquent_williamson_state.json (if present)

Bell/Williamson are optional — skipped if their local state file doesn't exist
(the first cloud run will self-seed). Otherwise this hands the cloud a baseline
from a known-good local run so the very first cloud diff is meaningful.

Auth: reads APIFY_TOKEN from .env (via python-dotenv).

Usage:
    PYTHONPATH=src python scripts/seed_apify_kvs.py
    PYTHONPATH=src python scripts/seed_apify_kvs.py --dry-run     # preview sizes
    PYTHONPATH=src python scripts/seed_apify_kvs.py --kvs-name foo  # custom store name
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEEN_IDS_PATH = PROJECT_ROOT / "seen_ids.json"
LAST_RUN_PATH = PROJECT_ROOT / "last_run.json"
TEXDEL_STATE_PATH = PROJECT_ROOT / "data" / "travis_tax_state" / "tax_delinquent_travis_state.json"

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
    args = parser.parse_args()

    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        print("ERROR: APIFY_TOKEN not set. Add it to .env or export it.", file=sys.stderr)
        return 1

    # Load local state
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
        print(
            f"ERROR: seen_ids.json is {type(seen_ids).__name__}, expected list",
            file=sys.stderr,
        )
        return 1

    if not isinstance(texdel_state, dict):
        print("ERROR: travis_texdel state is not a dict", file=sys.stderr)
        return 1

    # Summarize
    seen_count = len(seen_ids)
    texdel_apn_count = len(texdel_state.get("last_run_apns") or [])
    texdel_master_count = len(texdel_state.get("master_apns") or [])

    print(f"  last_run_date       : {last_run_date}")
    print(f"  seen_notice_ids     : {seen_count:,} keys")
    print(f"  travis_texdel_state : last_run_apns={texdel_apn_count:,}, master_apns={texdel_master_count:,}")
    for key, state in generic_states:
        apn_count = len(state.get("last_run_apns") or [])
        master_count = len(state.get("master_apns") or [])
        print(f"  {key:<19} : last_run_apns={apn_count:,}, master_apns={master_count:,}")
    skipped = [key for key, _ in GENERIC_TEXDEL if key not in {k for k, _ in generic_states}]
    for key in skipped:
        print(f"  {key:<19} : (no local state file — will self-seed on first cloud run)")

    if args.dry_run:
        print("\n[DRY RUN] No writes performed.")
        return 0

    # Connect + get/create the named KVS
    print(f"\nConnecting to Apify with token ...{token[-6:]}")
    client = ApifyClient(token)

    user = client.user().get()
    if not user:
        print("ERROR: APIFY_TOKEN invalid — /user endpoint returned empty", file=sys.stderr)
        return 1
    print(f"  authenticated as: {user.get('username', '?')} (id={user.get('id', '?')})")

    print(f"\nOpening named KVS: {args.kvs_name}")
    kvs_info = client.key_value_stores().get_or_create(name=args.kvs_name)
    kvs_id = kvs_info["id"]
    print(f"  KVS id: {kvs_id}")
    kvs = client.key_value_store(kvs_id)

    # Write the three records
    print("\nSeeding records...")
    kvs.set_record("last_run_date", last_run_date)
    print(f"  ✓ last_run_date = {last_run_date}")

    kvs.set_record("seen_notice_ids", seen_ids)
    print(f"  ✓ seen_notice_ids ({seen_count:,} keys)")

    kvs.set_record("travis_texdel_state", texdel_state)
    print(f"  ✓ travis_texdel_state (last_run_apns={texdel_apn_count:,})")

    for key, state in generic_states:
        kvs.set_record(key, state)
        print(f"  ✓ {key} (last_run_apns={len(state.get('last_run_apns') or []):,})")

    print("\nDone. The next Apify Actor run will pick up from this state.")
    print(f"Verify in Apify Console: Storage → Key-value stores → {args.kvs_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

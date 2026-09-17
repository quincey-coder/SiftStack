"""Re-sync the production schedule's runInput to the cleaned Actor input schema.

    python scripts/apify_resync_schedule_input.py            # dry run: prints the diff
    python scripts/apify_resync_schedule_input.py --apply    # PUT the migrated input

Why: the 2026-09-17 cleanup removed 19 inputs (obituary family, DirectSkip /
Trestle / Tracerfy, RAWPIPE alias, hidden knobs) and renamed 14 switches to
POSITIVE names (skip_zillow -> enrich_zillow, keep_listed ->
filter_listed_properties, ...). Apify ignores unknown keys, so a stale
schedule would silently run with defaults — and would keep sending a
retired vendor's secret. This migrates the stored body key-for-key: only
non-secret keys change, secret values (stored encrypted) round-trip untouched.
Backup of the pre-migration schedule: state_backups/apify_rollback_2026-09-17/.
"""

from __future__ import annotations

import json
import os
import sys

import requests
from dotenv import load_dotenv

SCHEDULE_ID = "F2qXIACaTvodoC9sy"

REMOVED = [
    "enable_obituary", "enable_ancestry", "skip_heir_verification", "skip_dm_address",
    "max_heir_depth", "obituary_workers", "obituary_deadline_secs", "serper_api_key",
    "firecrawl_api_key", "enable_directskip", "directskip_api_key", "enable_trestle",
    "trestle_api_key", "google_drive_reports_folder_id", "no_reports",
    "enable_tracerfy", "tracerfy_api_key", "upload_datasift_api", "proxy_country",
]
# old negative name -> new positive name (value inverts)
INVERTED = {
    "skip_smarty": "standardize_addresses",
    "skip_zillow": "enrich_zillow",
    "skip_tax": "enrich_cad_tax",
    "skip_geocode": "geocode_retry",
    "skip_zip_filter": "filter_investor_zips",
    "skip_condo_filter": "filter_condos",
    "keep_listed": "filter_listed_properties",
    "keep_government_records": "drop_government_owned",
    "skip_texdel_cleaner": "travis_texdel_cleaner",
    "datasift_no_enrich": "datasift_enrich_after_upload",
    "datasift_no_skip_trace": "datasift_skip_trace_after_upload",
}
# old name -> new name (same value)
RENAMED = {
    "exclude_vacant": "drop_vacant_land",
    "exclude_commercial": "drop_commercial",
    "exclude_entities": "drop_entity_owners",
}
SECRET_HINTS = ("password", "key", "token", "secret", "webhook", "email")


def migrate(d: dict) -> dict:
    out = dict(d)
    for k in REMOVED:
        out.pop(k, None)
    for old, new in INVERTED.items():
        if old in out:
            out[new] = not bool(out.pop(old))
        else:
            out.setdefault(new, True)
    for old, new in RENAMED.items():
        if old in out:
            out[new] = bool(out.pop(old))
    out.setdefault("use_residential_proxy", True)
    # The stored `datasift_no_skip_trace: true` dated from the RAWPIPE API
    # days ("$0.15/owner"). The wizard skip-trace is the unlimited plan, and
    # the owner wants it ON after every upload — do not carry the old value.
    out["datasift_skip_trace_after_upload"] = True
    return out


def _redact(d: dict) -> dict:
    return {k: ("***" if v and any(h in k for h in SECRET_HINTS) else v) for k, v in d.items()}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    apply = "--apply" in argv
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    tok = os.environ["APIFY_TOKEN"]
    url = f"https://api.apify.com/v2/schedules/{SCHEDULE_ID}"
    sched = requests.get(url, params={"token": tok}, timeout=60).json()["data"]
    action = sched["actions"][0]
    body = action["runInput"]["body"]
    old = json.loads(body) if isinstance(body, str) else body
    new = migrate(old)

    schema = json.load(open(".actor/input_schema.json", encoding="utf-8"))["properties"]
    unknown = sorted(k for k in new if k not in schema)
    print(f"schedule {SCHEDULE_ID}: {len(old)} keys -> {len(new)} keys; unknown to schema: {unknown}")
    for k in sorted(set(old) - set(new)):
        print(f"  - {k}")
    for k in sorted(set(new) - set(old)):
        print(f"  + {k} = {_redact(new)[k]}")
    for k in sorted(set(old) & set(new)):
        if old[k] != new[k]:
            print(f"  ~ {k}: {_redact(old)[k]} -> {_redact(new)[k]}")
    if not apply:
        print("dry run — pass --apply to write")
        return 0

    payload = {"actions": [
        {**action, "runInput": {**action["runInput"], "body": json.dumps(new)}}
    ]}
    r = requests.put(url, params={"token": tok}, json=payload, timeout=60)
    r.raise_for_status()
    back = requests.get(url, params={"token": tok}, timeout=60).json()["data"]
    b2 = back["actions"][0]["runInput"]["body"]
    got = json.loads(b2) if isinstance(b2, str) else b2
    assert sorted(got) == sorted(new), (sorted(set(got) ^ set(new)))
    print("APPLIED — schedule now carries", len(got), "keys; every removed key is gone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

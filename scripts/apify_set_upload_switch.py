"""Turn the scheduled run's DataSift auto-upload on or off.

    python scripts/apify_set_upload_switch.py --on            # dry run: prints the change
    python scripts/apify_set_upload_switch.py --on  --apply   # upload at the end of every run
    python scripts/apify_set_upload_switch.py --off --apply   # back to manual uploads

ON sets, in the production schedule's stored input:
    upload_datasift                   = true    (the upload wizard, Playwright)
    datasift_enrich_after_upload      = false   (upload only, like the manual batches)
    datasift_skip_trace_after_upload  = false   (owner rule 2026-10-21: never on upload)
Nothing else in the input changes; secrets round-trip untouched (stored
encrypted). The previous input body is backed up under state_backups/.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
TOK = os.environ["APIFY_TOKEN"]
API = "https://api.apify.com/v2"
SCHEDULE_ID = "F2qXIACaTvodoC9sy"
ACTOR = "TPcb87d3OJHlI6Yfy"


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    r = requests.get(f"{API}/schedules/{SCHEDULE_ID}", params={"token": TOK}, timeout=60)
    r.raise_for_status()
    sched = r.json()["data"]
    actions = sched.get("actions") or []
    act = next(x for x in actions if x.get("actorId") == ACTOR)
    body = json.loads((act.get("runInput") or {}).get("body") or "{}")

    want = {
        "upload_datasift": bool(a.on),
        "datasift_enrich_after_upload": False,
        "datasift_skip_trace_after_upload": False,
    }
    changes = {k: (body.get(k, "(not set)"), v) for k, v in want.items() if body.get(k, "(not set)") != v}
    print(f"schedule {sched.get('name')} ({SCHEDULE_ID}), cron {sched.get('cronExpression')} {sched.get('timezone')}")
    if not changes:
        print("already set — nothing to change")
        return
    for k, (old, new) in changes.items():
        print(f"  {k}: {old} -> {new}")
    if not a.apply:
        print("dry run — add --apply to write it")
        return

    bdir = ROOT / "state_backups" / "apify_schedule_input"
    bdir.mkdir(parents=True, exist_ok=True)
    bfile = bdir / f"schedule_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    bfile.write_text(json.dumps(sched, indent=1), encoding="utf-8")

    body.update(want)
    act["runInput"]["body"] = json.dumps(body)
    w = requests.put(f"{API}/schedules/{SCHEDULE_ID}", params={"token": TOK},
                     json={"actions": actions}, timeout=60)
    if w.status_code >= 400:
        print("FAILED:", w.status_code, w.text[:400])
        sys.exit(1)
    print("applied. backup:", bfile)


if __name__ == "__main__":
    main()

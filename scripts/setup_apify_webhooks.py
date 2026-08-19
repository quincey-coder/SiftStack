"""One-time setup: Apify platform webhooks → Slack for run-level failures.

The in-run health report (src/run_health.py) covers everything our code can
see — but a run that dies before/outside our code (OOM, Docker pull failure,
platform timeout, abort) never reaches that code. These platform webhooks are
the independent safety net: Apify itself POSTs to the Slack webhook when a
run FAILS, TIMES OUT, or is ABORTED.

Idempotent — safe to re-run; existing matching webhooks are left alone.

Usage (reads APIFY_TOKEN + SLACK_WEBHOOK_URL from .env):
    python scripts/setup_apify_webhooks.py            # create/verify
    python scripts/setup_apify_webhooks.py --list     # show current webhooks
    python scripts/setup_apify_webhooks.py --delete   # remove ours
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ACTOR_NAME = "sift-stack"

# One webhook per event type so a delivery failure of one doesn't hide others.
EVENT_TYPES = [
    "ACTOR.RUN.FAILED",
    "ACTOR.RUN.TIMED_OUT",
    "ACTOR.RUN.ABORTED",
]

# Slack incoming-webhook payload. Apify interpolates the {{variables}}.
PAYLOAD_TEMPLATE = (
    '{"text": ":rotating_light: *SiftStack Apify run {{eventType}}* — '
    'status {{resource.status}}\\n'
    '<https://console.apify.com/actors/runs/{{resource.id}}|Open run in Apify Console>"}'
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list existing webhooks and exit")
    ap.add_argument("--delete", action="store_true", help="delete the webhooks this script created")
    args = ap.parse_args()

    token = os.environ.get("APIFY_TOKEN", "")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not token:
        print("APIFY_TOKEN not set (env or .env)"); return 1

    from apify_client import ApifyClient
    client = ApifyClient(token)

    # Resolve the actor id by name (works for renamed accounts too).
    actor_id = None
    for actor in client.actors().list().items:
        name = actor.get("name") if isinstance(actor, dict) else getattr(actor, "name", None)
        if name == ACTOR_NAME:
            actor_id = actor.get("id") if isinstance(actor, dict) else getattr(actor, "id", None)
            break
    if not actor_id:
        print(f"Actor '{ACTOR_NAME}' not found on this account"); return 1
    print(f"Actor {ACTOR_NAME} = {actor_id}")

    existing = client.webhooks().list().items

    def _get(w, *keys):
        # apify-client 3.x returns models with snake_case attrs; older
        # versions returned camelCase dicts. Accept both.
        for key in keys:
            if isinstance(w, dict) and key in w:
                return w[key]
            if hasattr(w, key):
                return getattr(w, key)
        return None

    def _event_types(w) -> set[str]:
        raw = _get(w, "event_types", "eventTypes") or []
        return {str(getattr(e, "value", e)) for e in raw}

    def _actor_cond(w) -> str | None:
        cond = _get(w, "condition") or {}
        if isinstance(cond, dict):
            return cond.get("actorId") or cond.get("actor_id")
        return getattr(cond, "actor_id", None) or getattr(cond, "actorId", None)

    ours = [
        w for w in existing
        if _actor_cond(w) == actor_id and _event_types(w) & set(EVENT_TYPES)
    ]

    if args.list:
        for w in existing:
            print(f"- {_get(w, 'id')}  events={sorted(_event_types(w))}  "
                  f"actor={_actor_cond(w)}  "
                  f"url={str(_get(w, 'request_url', 'requestUrl'))[:60]}...")
        return 0

    if args.delete:
        for w in ours:
            client.webhook(_get(w, "id")).delete()
            print(f"Deleted webhook {_get(w, 'id')} ({_get(w, 'eventTypes')})")
        return 0

    if not slack_url:
        print("SLACK_WEBHOOK_URL not set (env or .env)"); return 1

    covered = set()
    for w in ours:
        covered.update(_event_types(w))

    created = 0
    for event_type in EVENT_TYPES:
        if event_type in covered:
            print(f"OK (exists): {event_type}")
            continue
        client.webhooks().create(
            event_types=[event_type],
            request_url=slack_url,
            payload_template=PAYLOAD_TEMPLATE,
            actor_id=actor_id,
        )
        created += 1
        print(f"Created: {event_type} → Slack")

    print(f"Done — {created} created, {len(EVENT_TYPES) - created} already existed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

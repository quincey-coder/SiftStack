#!/usr/bin/env python3
"""Quality run: every lead must have a task.

READ-ONLY audit of a DataSift (REISift) account. For each lead status it
counts how many records have **no open (incomplete) task** — i.e. leads
that have slipped through without a follow-up scheduled. Makes zero changes
to the CRM (the search runs as a GET via the API's method-override).

Mechanism (see src/datasift_api_client.py for the reverse-engineered API):
  - statuses  : GET  /api/internal/properties/status/      (lead stages)
  - count/list: POST /api/internal/property/  with header
                X-HTTP-Method-Override: GET and filter
                {any_property_status:[uuid], tasks_incomplete:[0,0]}
    where tasks_incomplete:[0,0] == "has no open task".

Usage:
  PYTHONPATH=src python scripts/audit_lead_tasks.py
  PYTHONPATH=src python scripts/audit_lead_tasks.py --all-statuses
  PYTHONPATH=src python scripts/audit_lead_tasks.py --list 20
  PYTHONPATH=src python scripts/audit_lead_tasks.py --mode owner   # experimental

Auth comes from the cached refresh token or DATASIFT_EMAIL/DATASIFT_PASSWORD
(same as the rest of the pipeline). Unset APIFY_TOKEN to run locally.
"""

from __future__ import annotations

import argparse
import sys

# Allow running as `python scripts/audit_lead_tasks.py` (adds src/ to path).
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from datasift_api_client import DataSiftAPIClient, DataSiftAPIError  # noqa: E402

# Filter meaning "the record has zero open/incomplete tasks".
NO_OPEN_TASK = [0, 0]

# Substrings that mark a status as a terminal / non-actionable stage — these
# are excluded from the default "must have a task" set. Everything else that
# looks like a lead/prospect stage is included.
_DEAD_MARKERS = (
    "dead", "lost", "not_interested", "dnc", "opt_out", "opt out",
    "closed", "sold", "under_contract", "under contract", "transaction",
    "listed", "close_out", "close out", "underwrite", "buyer", "refer",
    "push_deal", "push deal",
)
_LEAD_MARKERS = ("lead", "prospect", "follow", "ghosting", "new")


def is_active_lead(title: str) -> bool:
    """Heuristic: an actionable lead stage that should always carry a task."""
    t = (title or "").strip().lower()
    if any(m in t for m in _DEAD_MARKERS):
        return False
    return any(m in t for m in _LEAD_MARKERS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["property", "owner"], default="property",
                    help="Which record set to audit (default: property/seller leads).")
    ap.add_argument("--all-statuses", action="store_true",
                    help="Treat EVERY status as requiring a task, not just lead stages.")
    ap.add_argument("--list", type=int, default=0, metavar="N",
                    help="Print up to N example records that are missing a task.")
    args = ap.parse_args()

    try:
        client = DataSiftAPIClient.from_env()
    except (DataSiftAPIError, RuntimeError) as e:
        print(f"[FAIL] Could not authenticate: {e}", file=sys.stderr)
        return 1

    user = client.get_user()
    print(f"Account: {user.get('email')}  (plan: {user.get('plan_name')})")
    print(f"Mode: {args.mode}\n")

    if args.mode == "owner":
        print("[WARN] Owner-side task filtering is not yet verified — the owner "
              "search uses a different filter schema than property. Counts may "
              "reflect all records. Use --mode property for seller leads.\n")

    statuses = client.get_statuses(mode=args.mode)
    if not statuses:
        print("[WARN] No statuses returned for this account.")
        return 0

    # Decide which statuses are in scope for the "every lead has a task" rule.
    scoped = [
        s for s in statuses
        if args.all_statuses or is_active_lead(s.get("title", ""))
    ]
    if not scoped:
        print("[WARN] No lead-type statuses matched. Re-run with --all-statuses "
              "to audit every stage.")
        return 0

    print(f"{'STATUS':28} {'TOTAL':>8} {'NO TASK':>9} {'COVERAGE':>9}")
    print("-" * 58)

    grand_total = 0
    grand_missing = 0
    missing_examples: list[tuple[str, str]] = []

    for s in sorted(scoped, key=lambda x: (x.get("title") or "").lower()):
        uuid, title = s.get("uuid"), s.get("title") or "(untitled)"
        try:
            total = client.count_records({"any_property_status": [uuid]}, mode=args.mode)
            missing = client.count_records(
                {"any_property_status": [uuid], "tasks_incomplete": NO_OPEN_TASK},
                mode=args.mode,
            )
        except DataSiftAPIError as e:
            print(f"{title[:28]:28} {'ERR':>8}  ({e.status})")
            continue

        grand_total += total
        grand_missing += missing
        cov = "—" if total == 0 else f"{100 * (total - missing) / total:.0f}%"
        flag = "  ⚠" if missing else ""
        print(f"{title[:28]:28} {total:>8} {missing:>9} {cov:>9}{flag}")

        if args.list and missing and len(missing_examples) < args.list:
            res = client.search_records(
                {"any_property_status": [uuid], "tasks_incomplete": NO_OPEN_TASK},
                mode=args.mode, limit=min(args.list - len(missing_examples), 100),
            ).get("results", [])
            for r in res:
                addr = (r.get("property_address") or r.get("address")
                        or r.get("mailing_address") or r.get("uuid") or "?")
                missing_examples.append((title, str(addr)))

    print("-" * 58)
    cov = "—" if grand_total == 0 else f"{100 * (grand_total - grand_missing) / grand_total:.0f}%"
    print(f"{'TOTAL LEADS':28} {grand_total:>8} {grand_missing:>9} {cov:>9}")

    if grand_total == 0:
        print("\n✅ No lead records on this account yet — nothing to audit. "
              "The check is wired up and will report real numbers once leads load.")
    elif grand_missing == 0:
        print(f"\n✅ All {grand_total} leads have at least one open task. Clean.")
    else:
        print(f"\n⚠  {grand_missing} of {grand_total} leads have NO open task "
              f"and need one.")

    if missing_examples:
        print(f"\nExamples missing a task (first {len(missing_examples)}):")
        for title, addr in missing_examples:
            print(f"  [{title}] {addr}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

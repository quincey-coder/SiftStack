"""Build the "Deceased & Heir Intelligence" custom-field group in DataSift.

Creates the group + fields defined in deceased_heir_fields.json via the
internal API (see datasift_api_client.py custom-fields section for the
live-verified contract). Idempotent: existing labels are skipped, so
re-runs are safe and duplicate-label 400s are treated as skips.

Usage (from project root):
    python src/build_custom_fields.py --dry-run    # print what would be created
    python src/build_custom_fields.py              # create group + fields
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from datasift_api_client import DataSiftAPIClient, DataSiftAPIError

logger = logging.getLogger(__name__)

DEFINITIONS_PATH = Path(__file__).parent / "deceased_heir_fields.json"

# Server throttling: 150ms between creates avoids 429s; on 429 back off to 300ms.
CREATE_DELAY = 0.15
RETRY_DELAY = 0.3


def build(dry_run: bool = False) -> dict:
    defs = json.loads(DEFINITIONS_PATH.read_text())
    group_def = defs["group"]
    field_defs = defs["fields"]

    client = DataSiftAPIClient.from_env()

    # ── Resolve or create the group ──
    groups = client.list_custom_field_groups()
    existing = next(
        (g for g in groups if g.get("label") == group_def["label"]
         and g.get("entity_type") == group_def["entity_type"]),
        None,
    )
    if existing:
        group_id = existing["id"]
        logger.info("Group %r exists (id=%d)", group_def["label"], group_id)
    elif dry_run:
        group_id = -1
        logger.info("[dry-run] would create group %r", group_def["label"])
    else:
        created = client.create_custom_field_group(
            group_def["label"],
            entity_type=group_def["entity_type"],
            description=group_def.get("description", ""),
        )
        group_id = created["id"]
        logger.info("Created group %r (id=%d)", group_def["label"], group_id)

    # ── Create fields, skipping any label that already exists ──
    all_fields = client.list_custom_fields()
    existing_labels = {f["label"]: f for f in all_fields}

    stats = {"created": 0, "skipped": 0, "failed": 0}
    for fd in field_defs:
        label = fd["label"]
        if label in existing_labels:
            g = (existing_labels[label].get("group") or {})
            if g.get("id") != group_id:
                logger.warning(
                    "  SKIP %r — already exists in group %r (not ours); "
                    "resolve the collision manually", label, g.get("label"),
                )
            else:
                logger.info("  skip %r (exists)", label)
            stats["skipped"] += 1
            continue
        if dry_run:
            opts = f" options={fd['options']}" if fd.get("options") else ""
            logger.info("  [dry-run] would create %r (%s)%s", label, fd["field_type"], opts)
            stats["created"] += 1
            continue

        time.sleep(CREATE_DELAY)
        try:
            client.create_custom_field(
                label=label,
                field_type=fd["field_type"],
                group_id=group_id,
                entity_type=group_def["entity_type"],
                placeholder=fd.get("placeholder", ""),
                options=fd.get("options"),
            )
            logger.info("  created %r (%s)", label, fd["field_type"])
            stats["created"] += 1
        except DataSiftAPIError as e:
            if e.status == 429:
                time.sleep(RETRY_DELAY)
                try:
                    client.create_custom_field(
                        label=label,
                        field_type=fd["field_type"],
                        group_id=group_id,
                        entity_type=group_def["entity_type"],
                        placeholder=fd.get("placeholder", ""),
                        options=fd.get("options"),
                    )
                    logger.info("  created %r (%s) after 429 retry", label, fd["field_type"])
                    stats["created"] += 1
                    continue
                except DataSiftAPIError as e2:
                    e = e2
            if e.status == 400 and "label" in e.body.lower():
                logger.info("  skip %r (duplicate-label 400)", label)
                stats["skipped"] += 1
            else:
                logger.error("  FAILED %r: %d %s", label, e.status, e.body[:200])
                stats["failed"] += 1

    # ── Verify: re-fetch and diff against definitions ──
    if not dry_run:
        live = {
            f["label"]: f for f in client.list_custom_fields()
            if (f.get("group") or {}).get("id") == group_id
        }
        missing = [fd["label"] for fd in field_defs if fd["label"] not in live]
        wrong_type = [
            f'{fd["label"]} ({live[fd["label"]]["field_type"]} != {fd["field_type"]})'
            for fd in field_defs
            if fd["label"] in live and live[fd["label"]]["field_type"] != fd["field_type"]
        ]
        if missing or wrong_type:
            logger.error("VERIFY FAILED — missing: %s, wrong type: %s", missing, wrong_type)
            stats["verify"] = "FAILED"
        else:
            logger.info("VERIFY OK — all %d fields present with correct types", len(field_defs))
            stats["verify"] = "OK"

    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print without creating")
    args = parser.parse_args()
    result = build(dry_run=args.dry_run)
    print(f"\n{result}")
    sys.exit(1 if result.get("failed") or result.get("verify") == "FAILED" else 0)

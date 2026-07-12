"""Build the "Code Violation Cleanup" DataSift sequence.

The SiftStack side of resolved-code-violation handling has shipped since
2026-07-02 (see the code-enforcement resolution flow): resolved cases upload
as tag-only rows tagged "Code Violation Resolved" with a blank Lists column.
This is the missing DataSift half — the sequence that fires on that tag.

SCOPED cleanup, deliberately unlike the "Sold -> Reset" sequence:
  - Trigger:   property.tags.added
  - Condition: property has the "Code Violation Resolved" tag
  - Action:    remove ONLY the "Code Enforcement" list
  - NO status change, NO task clearing, NO other lists touched.

Because a resolved code violation kills exactly one distress signal. If the
property is still on Tax Delinquent / Probate / Lien, it keeps getting that
niche marketing (marketing gates on list membership + status!=Sold, verified
against niche_sequential.py). A status change would over-suppress a property
that is still a live lead on another signal.

Idempotent: get-or-creates the tag, skips the sequence if one with the same
title already exists.

Usage (from project root):
    python src/build_cleanup_sequence.py --dry-run
    python src/build_cleanup_sequence.py
"""

import argparse
import logging
import sys

from datasift_api_client import DataSiftAPIClient

logger = logging.getLogger(__name__)

SEQUENCE_TITLE = "Code Violation Cleanup"
RESOLVED_TAG = "Code Violation Resolved"
# The list SiftStack uploads code_violation records to
# (datasift_formatter.NOTICE_TYPE_TO_LIST["code_violation"]) — the account's
# built-in "Code Enforcement" list. Keep in sync with the formatter.
CODE_VIOLATION_LIST = "Code Enforcement"
FOLDER_TITLE = "default"  # alongside the "Sold -> Reset" cleanup sequence


def build(dry_run: bool = False) -> dict:
    client = DataSiftAPIClient.from_env()

    # ── Skip if the sequence already exists ──
    existing = next(
        (s for s in client.list_sequences(limit=200)
         if (s.get("title") or s.get("name")) == SEQUENCE_TITLE),
        None,
    )
    if existing:
        logger.info("Sequence %r already exists (uuid=%s) — nothing to do.",
                    SEQUENCE_TITLE, existing.get("uuid"))
        return {"created": False, "sequence": existing}

    # ── Resolve the trigger tag (get-or-create) ──
    if dry_run:
        logger.info("[dry-run] would get-or-create tag %r", RESOLVED_TAG)
        tag_uuid = "<tag-uuid>"
    else:
        tag = client.get_or_create_tag(RESOLVED_TAG)
        tag_uuid = tag.get("uuid")
        logger.info("Tag %r ready (uuid=%s)", RESOLVED_TAG, tag_uuid)

    # ── Resolve the target folder uuid ──
    folder_uuid = None
    try:
        d = client._request_json("GET", "/api/internal/sequence-folder/")
        folders = d.get("results") or d.get("data") or (d if isinstance(d, list) else [])
        match = next((f for f in folders if f.get("title") == FOLDER_TITLE), None)
        folder_uuid = match.get("uuid") if match else None
    except Exception as e:
        logger.warning("Could not resolve folder %r (%s) — creating unfoldered", FOLDER_TITLE, e)

    conditions = [{
        "condition": "has_all",
        "payload": {
            "field": "tags_uuid",
            "label": "property.tags.added",
            "values": [tag_uuid],
            "resource": False,
        },
    }]
    actions = [{
        "action": "remove",
        "payload": {
            "field": "lists",
            "label": "property.lists.remove",
            "values": [CODE_VIOLATION_LIST],
        },
    }]

    if dry_run:
        logger.info("[dry-run] would create sequence %r", SEQUENCE_TITLE)
        logger.info("  trigger:   property.tags.added")
        logger.info("  condition: has tag %r", RESOLVED_TAG)
        logger.info("  action:    remove list %r (and nothing else)", CODE_VIOLATION_LIST)
        logger.info("  folder:    %r (uuid=%s)", FOLDER_TITLE, folder_uuid)
        return {"created": False, "dry_run": True}

    created = client.create_sequence(
        title=SEQUENCE_TITLE,
        trigger="property.tags.added",
        conditions=conditions,
        actions=actions,
        folder_uuid=folder_uuid,
    )
    seq_id = created.get("uuid") or created.get("id")
    logger.info("Created sequence %r (uuid=%s)", SEQUENCE_TITLE, seq_id)

    # ── Verify by reading it back ──
    fetched = client.get_sequence(seq_id)
    ok = (
        fetched.get("trigger") == "property.tags.added"
        and fetched.get("is_active", True)
        and any(
            a.get("payload", {}).get("field") == "lists"
            and CODE_VIOLATION_LIST in (a.get("payload", {}).get("values") or [])
            for a in fetched.get("actions", [])
        )
        and not any(
            a.get("action") in ("set-field-value", "property-tasks-delete-all",
                                 "property-assign")
            for a in fetched.get("actions", [])
        )
    )
    if ok:
        logger.info("VERIFY OK — scoped list-remove sequence is live and active")
    else:
        logger.error("VERIFY FAILED — check the sequence in the UI: %s", fetched.get("actions"))
    return {"created": True, "verify": "OK" if ok else "FAILED", "sequence_id": seq_id}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print without creating")
    args = parser.parse_args()
    result = build(dry_run=args.dry_run)
    print(f"\n{result}")
    sys.exit(1 if result.get("verify") == "FAILED" else 0)

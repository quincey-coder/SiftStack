"""Completeness-gated dedup seal (owner rule 2026-09-09).

A record is sealed into seen_notice_ids ONLY when it has both an owner
identity and a property address; incomplete records stay open for the rolling
lookback re-scan. Sold tag-update rows are never sealed.

Run: PYTHONPATH=src python tests/test_dedup_completeness.py   (or pytest)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import _is_complete_for_seen, _seal_seen_ids  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


def _n(**kw) -> NoticeData:
    base = dict(notice_type="foreclosure", county="Travis", state="TX")
    base.update(kw)
    return NoticeData(**base)


def test_foreclosure_owner_and_address_complete():
    assert _is_complete_for_seen(_n(owner_name="Ann Lee", address="1 Main St", zip="78701"))


def test_address_only_incomplete():
    assert not _is_complete_for_seen(_n(address="1 Main St", zip="78701"))


def test_owner_only_incomplete():
    assert not _is_complete_for_seen(_n(owner_name="Ann Lee"))


def test_address_without_city_or_zip_incomplete():
    assert not _is_complete_for_seen(_n(owner_name="Ann Lee", address="1 Main St"))


def test_city_suffices_for_address():
    assert _is_complete_for_seen(_n(owner_name="Ann Lee", address="1 Main St", city="Austin"))


def test_tax_owner_name_counts():
    assert _is_complete_for_seen(_n(tax_owner_name="LEE ANN", address="1 Main St", zip="78701"))


def test_business_name_counts():
    assert _is_complete_for_seen(_n(business_name="Acme LLC", address="1 Main St", zip="78701"))


def test_probate_decedent_and_address_complete():
    assert _is_complete_for_seen(
        _n(notice_type="probate", decedent_name="Joe Doe", address="1 Main St", zip="78701")
    )


def test_probate_decedent_only_incomplete():
    assert not _is_complete_for_seen(_n(notice_type="probate", decedent_name="Joe Doe"))


def test_probate_decision_maker_counts():
    assert _is_complete_for_seen(
        _n(notice_type="probate", decision_maker_name="Sue Doe", address="1 Main St", city="Austin")
    )


def test_lien_debtor_and_address_complete():
    assert _is_complete_for_seen(_n(notice_type="lien", owner_name="Bob Ray", address="9 Elm", zip="76541"))


def test_sold_never_complete():
    n = _n(owner_name="Ann Lee", address="1 Main St", zip="78701")
    n.record_status = "sold"
    assert not _is_complete_for_seen(n)


def test_whitespace_names_do_not_count():
    assert not _is_complete_for_seen(_n(owner_name="   ", address="1 Main St", zip="78701"))


def test_seal_counts_and_mutation():
    complete = _n(owner_name="Ann Lee", address="1 Main St", zip="78701")
    complete._dedup_key = "id:1"
    open_rec = _n(address="2 Main St", zip="78701")
    open_rec._dedup_key = "id:2"
    dup = _n(owner_name="Ann Lee", address="1 Main St", zip="78701")
    dup._dedup_key = "id:1"
    sold = _n(owner_name="X Y", address="3 Main St", zip="78701")
    sold.record_status = "sold"
    sold._dedup_key = "id:3"
    seen: set = set()
    added, left_open = _seal_seen_ids([complete, open_rec, dup, sold], seen)
    assert added == 1, added
    assert left_open == 1, left_open
    assert seen == {"id:1"}, seen


def test_seal_all_escape_hatch_seals_incomplete_but_not_sold():
    open_rec = _n(address="2 Main St", zip="78701")
    open_rec._dedup_key = "id:2"
    sold = _n(owner_name="X Y", address="3 Main St", zip="78701")
    sold.record_status = "sold"
    sold._dedup_key = "id:3"
    seen: set = set()
    added, left_open = _seal_seen_ids([open_rec, sold], seen, require_complete=False)
    assert added == 1 and left_open == 0
    assert seen == {"id:2"}


def test_seal_recomputes_key_when_snapshot_missing():
    n = _n(owner_name="Ann Lee", address="1 Main St", zip="78701", parcel_id="P123")
    seen: set = set()
    added, _ = _seal_seen_ids([n], seen)
    assert added == 1 and seen == {"parcel:P123"}, seen


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

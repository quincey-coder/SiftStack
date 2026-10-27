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


# ── Degenerate keys (no discriminator) — never sealed, never matched ─────────

def test_is_degenerate_key():
    from main import _is_degenerate_key
    assert _is_degenerate_key("addr:probate|williamson||")
    assert _is_degenerate_key("addr:lien|travis||")
    assert _is_degenerate_key("addr:lien|bell| |")           # whitespace-only discriminator
    assert not _is_degenerate_key("addr:lien|travis|john smith|")
    assert not _is_degenerate_key("addr:foreclosure|travis|1 main st|78701")
    assert not _is_degenerate_key("parcel:000000007010")
    assert not _is_degenerate_key("id:www.tccsearch.org/instrument/202641242")
    assert not _is_degenerate_key("")


def test_degenerate_addr_key_never_sealed_even_when_complete():
    """Snapshot taken before enrichment (no address/name) → degenerate key.
    Enrichment later fills the address so the record IS complete — the old code
    sealed `addr:probate|williamson||` here and blanked the whole feed."""
    n = _n(notice_type="probate", county="Williamson", decedent_name="Joe Doe",
           address="1 Main St", zip="78664")
    n._dedup_key = "addr:probate|williamson||"
    seen: set = set()
    added, left_open = _seal_seen_ids([n], seen)
    assert added == 0 and left_open == 1
    assert seen == set()


def test_degenerate_key_not_sealed_by_escape_hatch():
    n = _n(notice_type="lien", county="Travis", owner_name="Bob Ray", address="9 Elm", zip="78701")
    n._dedup_key = "addr:lien|travis||"
    seen: set = set()
    added, _ = _seal_seen_ids([n], seen, require_complete=False)
    assert added == 0 and seen == set()


def test_purge_degenerate_keys_on_load():
    from main import _purge_degenerate_keys
    seen = {"addr:lien|travis||", "addr:lien|bell||", "parcel:1", "addr:lien|travis|bob ray|"}
    removed = _purge_degenerate_keys(seen)
    assert removed == 2
    assert seen == {"parcel:1", "addr:lien|travis|bob ray|"}


def test_dedup_key_for_unlocated_probate_is_degenerate_and_recognised():
    """The key the pipeline actually builds for a probate with no address/owner
    at snapshot time is degenerate — the guard must recognise that exact shape."""
    from main import _notice_dedup_key, _is_degenerate_key
    n = _n(notice_type="probate", county="Williamson", decedent_name="Joe Doe")
    key = _notice_dedup_key(n)
    assert key == "addr:probate|williamson||", key
    assert _is_degenerate_key(key)


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

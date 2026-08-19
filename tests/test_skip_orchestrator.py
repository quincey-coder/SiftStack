"""Tests for the multi-provider skip-trace orchestrator (offline, no spend).

    PYTHONPATH=src python tests/test_skip_orchestrator.py
    PYTHONPATH=src pytest tests/test_skip_orchestrator.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import skip_orchestrator as so  # noqa: E402
import smartskip as ss  # noqa: E402
import directskip as ds  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────

def _ss_record():
    """SmartSkip: owner mobile (shared with DS) + a child relative with relationship."""
    r = ss.SmartSkipRecord(first="Jane", last="Doe", property_address="123 Main St",
                           property_city="Austin", property_state="TX", property_zip="78701",
                           deceased_flag=False)
    r.subject_phones = [{"number": "5125550101", "type": "Mobile"},
                        {"number": "5125550150", "type": "Mobile"}]  # 0150 = SS-only
    r.relatives = [ss.SmartSkipPerson(first="John", last="Doe", name="John Doe",
                                      relationship="child", age="45",
                                      phones=[{"number": "5125550202", "type": "Mobile"}])]
    return r


def _ds_record():
    """DirectSkip: owner mobile (0101 shared) + a landline + same child, no relationship."""
    r = ds.DirectSkipRecord(first="Jane", last="Doe", property_address="123 Main St",
                            property_city="Austin", property_state="TX", property_zip="78701",
                            result_code="CI", source="api")
    r.subject_phones = [{"number": "5125550101", "type": "mobile"},
                        {"number": "5125550199", "type": "landline"}]  # 0199 = DS-only
    r.subject_emails = ["jane@example.com"]
    r.relatives = [ds.DirectSkipPerson(first="John", last="Doe", name="John Doe",
                                       relationship="relative", age="45",
                                       phones=[{"number": "5125550303", "type": "mobile"}])]
    return r


def _notice():
    return NoticeData(owner_name="Jane Doe", address="123 Main St", city="Austin",
                      state="TX", zip="78701")


# ── loader ────────────────────────────────────────────────────────────

def test_loader_reads_datasift_export_and_flags_entities():
    csv_text = (
        "Property Street Address,Property City,Property State,Property ZIP Code,"
        "Owner First Name,Owner Last Name,Mailing Street Address,Mailing City,"
        "Mailing State,Mailing ZIP Code,FULL NAME/COMPANY/TRUST,APN\n"
        "123 Main St,Austin,TX,78701,Jane,Doe,PO Box 5,Austin,TX,78701,,R12345\n"
        "9 Oak Dr,Austin,TX,78704,,,9 Oak Dr,Austin,TX,78704,ACME HOLDINGS LLC,R99\n"
    )
    p = _tmp(csv_text)
    notices = so.load_datasift_export(p)
    assert len(notices) == 2
    assert notices[0].owner_name == "Jane Doe" and notices[0].parcel_id == "R12345"
    assert notices[0].mailing_address == "PO Box 5"
    assert so._is_eligible(notices[0]) and not so._is_eligible(notices[1])  # LLC excluded


# ── estimate ──────────────────────────────────────────────────────────

def test_estimate_math():
    notices = [_notice() for _ in range(10)]
    est = so.estimate(notices)
    assert est["eligible"] == 10
    assert est["smartskip"] == 1.50            # 10 * 0.15
    assert est["directskip_ceiling"] == 1.00   # 10 * 0.10
    assert est["trestle_low"] == round(10 * 6 * 0.015, 2)
    assert est["total_high"] == round(1.50 + 1.00 + 10 * 14 * 0.015, 2)


# ── merge / provenance ────────────────────────────────────────────────

def test_merge_provenance_sets():
    rec = so.merge_record(_notice(), _ss_record(), _ds_record())
    owner_by_digits = {mp.digits: mp for mp in rec.owner.phones}
    assert owner_by_digits["5125550101"].providers == {"SmartSkip", "DirectSkip"}  # both
    assert owner_by_digits["5125550150"].providers == {"SmartSkip"}                # SS-only
    assert owner_by_digits["5125550199"].providers == {"DirectSkip"}               # DS-only
    assert owner_by_digits["5125550199"].ptype == "landline"
    assert rec.emails == ["jane@example.com"]
    assert rec.providers == {"SmartSkip", "DirectSkip"}


def test_merge_relative_relationship_from_smartskip():
    rec = so.merge_record(_notice(), _ss_record(), _ds_record())
    john = next(p for p in rec.others if p.name == "John Doe")
    # John is returned by both; SmartSkip's "child" relationship must win.
    assert john.relationship == "child" and john.relationship_confirmed
    assert john.providers == {"SmartSkip", "DirectSkip"}
    # Both of John's numbers merged onto the one person.
    assert {mp.digits for mp in john.phones} == {"5125550202", "5125550303"}


def test_merge_titlecases_allcaps_vendor_names():
    # DirectSkip returns ALL-CAPS; the merge must title-case (ALL-CAPS = red flag).
    r = ds.DirectSkipRecord(first="SHEILA", last="ANZALONE", property_address="1 A St",
                            result_code="CI", source="api")
    r.subject_phones = [{"number": "5125550101", "type": "mobile"}]
    r.relatives = [ds.DirectSkipPerson(first="CHRISTINA", last="ZACHMAN",
                                       name="CHRISTINA ZACHMAN", relationship="relative",
                                       phones=[{"number": "5125550202", "type": "mobile"}])]
    rec = so.merge_record(NoticeData(owner_name="SHEILA ANZALONE", address="1 A St"), None, r)
    assert rec.owner.name == "Sheila Anzalone"
    assert rec.others[0].name == "Christina Zachman"


def test_merge_directskip_address_only_never_owner():
    dsr = ds.DirectSkipRecord(first="Donald", last="Smith", property_address="9 Oak Dr",
                              result_code="AB1", address_only_match=True, source="api")
    dsr.subject_phones = [{"number": "5125550400", "type": "mobile"}]
    rec = so.merge_record(NoticeData(owner_name="Barbara Smith", address="9 Oak Dr"),
                          None, dsr)
    assert rec.owner.phones == []                       # not attributed to the owner
    unver = next(p for p in rec.others if p.role == "unverified")
    assert unver.phones[0].digits == "5125550400"


# ── eviction ──────────────────────────────────────────────────────────

def test_eviction_keeps_dial_first_second_cuts_worst():
    def ph(d, tier, valid=True):
        return so.MergedPhone(digits=d, tier=tier, valid=valid)
    phones = [ph("1111111111", "Drop"), ph("2222222222", "Dial First"),
              ph("3333333333", "Dial Fourth"), ph("4444444444", "Dial Second"),
              ph("5555555555", "Unknown"), ph("6666666666", "Dial First")]
    kept, cut = so.select_survivors(phones, cap=3)
    kept_d = {p.digits for p in kept}
    assert "2222222222" in kept_d and "6666666666" in kept_d and "4444444444" in kept_d
    cut_d = {p.digits for p in cut}
    assert cut_d == {"1111111111", "3333333333", "5555555555"}  # Drop + Fourth + Unknown


def test_invalid_number_cut_first():
    def ph(d, tier, valid=True):
        return so.MergedPhone(digits=d, tier=tier, valid=valid)
    phones = [ph("1111111111", "Dial First", valid=False), ph("2222222222", "Drop"),
              ph("3333333333", "Dial Third")]
    kept, cut = so.select_survivors(phones, cap=2)
    assert "1111111111" in {p.digits for p in cut}  # invalid evicted despite Dial First tier


# ── notes ─────────────────────────────────────────────────────────────

def test_notes_label_provider_tier_and_discrepancies():
    rec = so.merge_record(_notice(), _ss_record(), _ds_record())
    # Give the shared owner mobile a tier so the label shows.
    for mp in rec.owner.phones:
        if mp.digits == "5125550101":
            mp.tier, mp.score, mp.valid = "Dial First", 92, True
    ordered = so._ordered_unique_phones(rec)
    kept, cut = so.select_survivors(ordered, 30)
    slot_of = {mp.digits: i for i, mp in enumerate(kept, 1)}
    notes = so.build_notes(rec, slot_of, cut, "08/2026")
    assert "[Dial First 92] [SmartSkip+DirectSkip]" in notes
    assert "RELATIVE - child: John Doe" in notes
    assert "Only SmartSkip found: 512-555-0150" in notes
    assert "Only DirectSkip found: 512-555-0199" in notes
    assert "PROVENANCE / DISCREPANCIES" in notes


# ── write + tags ──────────────────────────────────────────────────────

def test_write_upload_csv_slots_and_tags():
    import csv as _csv
    rec = so.merge_record(_notice(), _ss_record(), _ds_record())
    out = _tmp("", ext="csv")
    stats = so.write_upload_csv([rec], out, phone_cap=30, stamp="08/2026")
    assert stats["records"] == 1
    with open(out, newline="", encoding="utf-8") as fh:
        row = next(_csv.DictReader(fh))
    # 5 unique numbers across owner + relative → 5 slots.
    slots = [row[f"Phone {i}"] for i in range(1, 31) if row[f"Phone {i}"]]
    assert set(slots) == {"5125550101", "5125550150", "5125550199", "5125550202", "5125550303"}
    assert "SmartSkip" in row["Tags"] and "DirectSkip" in row["Tags"] and "living" in row["Tags"]
    assert row["Owner First Name"] == "Jane" and row["Owner Last Name"] == "Doe"
    assert row["Email 1"] == "jane@example.com"


def test_litigator_number_withheld_from_slots_but_in_notes():
    import csv as _csv
    rec = so.merge_record(_notice(), _ss_record(), _ds_record())
    # Flag the owner's shared mobile as a litigator risk.
    for mp in rec.owner.phones:
        mp.tier, mp.score, mp.valid = "Dial First", 95, True
        if mp.digits == "5125550101":
            mp.litigator = True
    out = _tmp("", ext="csv")
    stats = so.write_upload_csv([rec], out, phone_cap=30, stamp="08/2026")
    with open(out, newline="", encoding="utf-8") as fh:
        row = next(_csv.DictReader(fh))
    slots = {row[f"Phone {i}"] for i in range(1, 31) if row[f"Phone {i}"]}
    assert "5125550101" not in slots                      # litigator NOT in a dial slot
    assert "5125550150" in slots                          # other numbers still uploaded
    assert stats["litigator_withheld"] == 1
    assert "LITIGATOR - DO NOT CALL" in row["Notes"] and "512-555-0101" in row["Notes"]
    assert "litigator" not in row["Tags"].lower()         # per user: no record-level tag


def test_score_all_respects_budget(monkeypatch):
    rec = so.merge_record(_notice(), _ss_record(), _ds_record())  # 5 unique numbers
    captured = {}

    def fake_process(phones, api_key, **kw):
        captured["n"] = len(phones)
        return ([{"phone_number": p[1], "activity_score": 90, "assigned_tag": "Dial First",
                  "is_valid": True, "is_litigator_risk": False} for p in phones], [])

    monkeypatch.setattr(so, "process_phones", fake_process)
    # Budget only covers 2 numbers (2 * 0.015 = 0.03).
    score_map = so.score_all([rec], budget=0.03, api_key="k")
    assert captured["n"] == 2 and len(score_map) == 2


# ── harness ───────────────────────────────────────────────────────────

_TMP = []


def _tmp(text, ext="csv"):
    import tempfile
    fd, path = tempfile.mkstemp(suffix="." + ext)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    _TMP.append(path)
    return path


class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def _run():
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = 0
    for name, fn in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(mp)
            else:
                fn()
            print(f"  [PASS] {name}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            if os.environ.get("TB"):
                traceback.print_exc()
        finally:
            mp.undo()
    for p in _TMP:
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"\n  {passed}/{len(tests)} tests passed.")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)

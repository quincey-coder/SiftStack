"""Tests for the DirectSkip client — API + portal parsing, apply, guards.

Offline only: HTTP is monkeypatched, no live calls, no spend. Fixtures are
synthetic (no PII) and encode both result shapes the parser must handle.

    PYTHONPATH=src python tests/test_directskip.py
    PYTHONPATH=src pytest tests/test_directskip.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import directskip as ds  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────

def _api_ci_match():
    """A clean CI match: owner + mobile/landline + email + 2 relatives."""
    return {
        "status": {"error": ""},
        "input": {"firstname": "Jane", "lastname": "Doe",
                  "property_address": "123 Main St", "property_city": "Austin",
                  "property_state": "TX", "property_zip": "78701",
                  "address": "123 Main St", "city": "Austin", "state": "TX", "zip": "78701"},
        "result_code": ["CI"],
        "contacts": [{
            "names": [{"firstname": "Jane", "lastname": "Doe", "age": "71", "deceased": "N"}],
            "phones": [{"phonenumber": "512-555-0101", "phonetype": "Mobile"},
                       {"phonenumber": "(512) 555-0199", "phonetype": "Residential"}],
            "emails": [{"email": "Jane@Example.com"}],
            "confirmed_address": [{"address": "123 Main St", "city": "Austin",
                                   "state": "TX", "zip": "78701"}],
            "relatives": [
                {"name": "Doe, John", "age": "45",
                 "phones": [{"phonenumber": "512-555-0202", "phonetype": "Mobile"}]},
                {"name": "Mary Doe", "age": "43",
                 "phones": [{"phonenumber": "5125550303", "phonetype": "Mobile"}]},
            ],
        }],
    }


def _api_no_match():
    return {"status": {"error": ""}, "input": {"firstname": "Zzz", "lastname": "Qqq"},
            "result_code": [], "contacts": []}


def _api_ab_match():
    """AB1: address-only match — the returned person is NOT the input owner."""
    return {
        "status": {"error": ""},
        "input": {"firstname": "Barbara", "lastname": "Smith",
                  "property_address": "9 Oak Dr", "property_city": "Austin",
                  "property_state": "TX", "property_zip": "78704"},
        "result_code": ["AB1"],
        "contacts": [{
            "names": [{"firstname": "Donald", "lastname": "Smith", "age": "60", "deceased": "N"}],
            "phones": [{"phonenumber": "512-555-0400", "phonetype": "Mobile"}],
            "relatives": [],
        }],
    }


def _api_deceased():
    return {
        "status": {"error": ""},
        "input": {"firstname": "Sam", "lastname": "Gray", "property_address": "5 Elm",
                  "property_city": "Austin", "property_state": "TX", "property_zip": "78702"},
        "result_code": ["CI"],
        "contacts": [{
            "names": [{"firstname": "Sam", "lastname": "Gray", "age": "88", "deceased": "Y"}],
            "phones": [{"phonenumber": "512-555-0500", "phonetype": "Mobile"}],
            "relatives": [{"name": "Gray, Paula", "age": "55",
                           "phones": [{"phonenumber": "512-555-0501", "phonetype": "Mobile"}]}],
        }],
    }


_CSV_HEADER = (
    "Input First Name,Input Last Name,Input Mailing Address,Input Mailing City,"
    "Input Mailing State,Input Mailing Zip,Input Property Address,Input Property City,"
    "Input Property State,Input Property Zip,ResultCode,Matched First Name,Matched Last Name,"
    "Age,Deceased,Phone1,Phone1 Type,Phone2,Phone2 Type,Email1,"
    "Relative1 Name,Relative1 Age,Relative1 Phone1,Relative1 Phone1 Type,"
    "Person2 First Name,Person2 Last Name,Person2 Age,Person2 Deceased,Person2 Phone1,Person2 Phone1 Type"
)


def _csv_fixture():
    row = ("Jane,Doe,123 Main St,Austin,TX,78701,123 Main St,Austin,TX,78701,"
           "CI,Jane,Doe,71,N,512-555-0101,Mobile,512-555-0199,Residential,jane@example.com,"
           "\"Doe, John\",45,512-555-0202,Mobile,"
           "Bob,Doe,50,N,512-555-0700,Mobile")
    return _CSV_HEADER + "\n" + row + "\n"


# ── parse tests ───────────────────────────────────────────────────────

def test_parse_api_ci_match():
    rec = ds.parse_api_response(_api_ci_match())
    assert rec.first == "Jane" and rec.last == "Doe"
    assert rec.result_code == "CI" and not rec.address_only_match
    assert [p["number"] for p in rec.subject_phones] == ["5125550101", "5125550199"]
    assert rec.subject_phones[0]["type"] == "mobile"
    assert rec.subject_phones[1]["type"] == "landline"
    assert rec.subject_emails == ["jane@example.com"]  # lowercased
    names = {p.name for p in rec.relatives}
    assert names == {"John Doe", "Mary Doe"}  # 'Doe, John' comma-split correctly
    assert rec.has_results and not rec.no_match


def test_parse_api_no_match_is_free_shape():
    rec = ds.parse_api_response(_api_no_match())
    assert rec.no_match and not rec.has_results
    assert rec.subject_phones == [] and rec.relatives == []


def test_parse_result_code_variants():
    assert ds._result_code_str("ci") == "CI"
    assert ds._result_code_str(["CI"]) == "CI"
    assert ds._result_code_str([{"result_code": "AB1"}]) == "AB1"
    assert ds._result_code_str({"result_code": "cI"}) == "CI"
    assert ds._result_code_str([]) == ""


def test_parse_csv_matches_api_shape():
    rec = ds.parse_export(_write_tmp(_csv_fixture(), "csv"))[0]
    assert rec.source == "csv"
    assert rec.first == "Jane" and rec.last == "Doe" and rec.result_code == "CI"
    assert [p["number"] for p in rec.subject_phones] == ["5125550101", "5125550199"]
    assert "jane@example.com" in rec.subject_emails
    # John (relative) + Bob (Person2) both land in the relative graph
    assert {p.name for p in rec.relatives} == {"John Doe", "Bob Doe"}


def test_parse_export_rejects_wrong_layout():
    bad = _write_tmp("Foo,Bar\n1,2\n", "csv")
    try:
        ds.parse_export(bad)
    except ds.DirectSkipError as e:
        assert "misalign" in str(e)
    else:
        raise AssertionError("expected DirectSkipError on missing required columns")


# ── apply tests ───────────────────────────────────────────────────────

def test_apply_owner_phones_to_slots_relatives_to_heirmap():
    rec = ds.parse_api_response(_api_ci_match())
    n = NoticeData(owner_name="Jane Doe", address="123 Main St", city="Austin",
                   state="TX", zip="78701")
    stats = ds.apply_to_notice(n, rec)
    assert n.mobile_1 == "5125550101" and n.landline_1 == "5125550199"
    assert n.primary_phone == "5125550101" and n.email_1 == "jane@example.com"
    hm = json.loads(n.heir_map_json)
    assert all(h["source"] == "directskip" for h in hm)
    # No relative number ever reaches an owner dial slot.
    owner_slots = {n.mobile_1, n.mobile_2, n.mobile_3,
                   n.landline_1, n.landline_2, n.landline_3, n.primary_phone}
    assert "5125550202" not in owner_slots and "5125550303" not in owner_slots
    assert stats["heirs"] == 2


def test_apply_address_only_match_never_becomes_owner():
    rec = ds.parse_api_response(_api_ab_match())
    assert rec.address_only_match
    n = NoticeData(owner_name="Barbara Smith", address="9 Oak Dr", city="Austin",
                   state="TX", zip="78704")
    ds.apply_to_notice(n, rec)
    # The returned (different) person's phone must NOT fill the owner slots,
    # and must NOT be promoted to decision maker.
    assert n.mobile_1 == "" and n.primary_phone == ""
    assert n.decision_maker_name == ""


def test_apply_deceased_is_observation_not_verdict():
    rec = ds.parse_api_response(_api_deceased())
    assert rec.deceased_flag
    n = NoticeData(owner_name="Sam Gray", address="5 Elm", city="Austin",
                   state="TX", zip="78702")
    ds.apply_to_notice(n, rec)
    assert n.smartskip_deceased_flag == "yes"     # observation recorded
    assert n.owner_deceased == "" and n.date_of_death == ""  # never asserted here


# ── client / cost-guard tests (HTTP mocked) ───────────────────────────

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_search_contact_raises_on_status_error(monkeypatch):
    client = ds.DirectSkipClient(api_key="k")

    def fake_post(url, **kw):
        return _FakeResp({"status": {"error": "invalid api key"}, "contacts": []})

    monkeypatch.setattr(ds.requests, "post", fake_post)
    try:
        client.search_contact(first="A", last="B", mailing_address="", mailing_city="",
                              mailing_state="TX", mailing_zip="", property_address="x",
                              property_city="", property_state="TX", property_zip="")
    except ds.DirectSkipError as e:
        assert "status.error" in str(e)
    else:
        raise AssertionError("expected DirectSkipError on status.error")


def test_batch_cost_cap_stops_and_no_match_is_free(monkeypatch):
    # 5 records; a $0.25 cap at $0.10/hit allows only 2 paid hits. Record #3 is a
    # no-match (free) and must NOT count against the cap.
    payloads = [_api_ci_match(), _api_ci_match(), _api_no_match(),
                _api_ci_match(), _api_ci_match()]
    calls = {"n": 0}

    def fake_post(url, **kw):
        i = calls["n"]
        calls["n"] += 1
        return _FakeResp(payloads[i] if i < len(payloads) else _api_no_match())

    monkeypatch.setattr(ds.requests, "post", fake_post)
    client = ds.DirectSkipClient(api_key="k")
    notices = [NoticeData(owner_name=f"Jane Doe", address=f"{i} Main St",
                          city="Austin", state="TX", zip="78701") for i in range(5)]
    stats = client.batch_search(notices, max_cost=0.25, delay=0)
    assert stats["cost"] <= 0.25 and stats["cap_reached"]
    assert stats["matched"] == 2          # two paid hits, then capped
    assert stats["cost"] == 0.20


def test_batch_skips_entities(monkeypatch):
    monkeypatch.setattr(ds.requests, "post",
                        lambda url, **kw: _FakeResp(_api_no_match()))
    client = ds.DirectSkipClient(api_key="k")
    ent = NoticeData(owner_name="Acme LLC", address="1 A St", city="Austin",
                     state="TX", zip="78701")
    ent.entity_type = "llc"
    ent.business_name = "Acme LLC"
    stats = client.batch_search([ent], max_cost=5.0, delay=0)
    assert stats["entities_skipped"] == 1 and stats["traced"] == 0


# ── harness ───────────────────────────────────────────────────────────

_TMP = []


def _write_tmp(text, ext):
    import tempfile
    fd, path = tempfile.mkstemp(suffix="." + ext)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    _TMP.append(path)
    return path


class _MonkeyPatch:
    """Minimal monkeypatch stand-in so tests run without pytest installed."""
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
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
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

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


# ── approval gate (hard stop before any spend) ────────────────────────

def _people(n=3):
    return [NoticeData(owner_name=f"Jane Doe{i}", address=f"{100 + i} Main St",
                       city="Austin", state="TX", zip="78704") for i in range(n)]


def _forbid(*a, **k):
    raise AssertionError("a paid vendor path was reached without approval")


def _no_vendors(monkeypatch):
    monkeypatch.setattr(so.smartskip, "SmartSkipClient", _forbid)
    monkeypatch.setattr(so.directskip, "DirectSkipClient", _forbid)
    monkeypatch.setattr(so, "score_all", _forbid)


def test_run_refuses_without_approval(monkeypatch):
    _no_vendors(monkeypatch)
    for bogus in (None, 50, {"max_cost": 50}):
        try:
            so.run(_people(), bogus)
        except so.OrchestratorError as e:
            assert "Nothing was spent" in str(e)
        else:
            raise AssertionError(f"run() spent with approval={bogus!r}")


def test_run_refuses_approval_for_a_different_batch(monkeypatch):
    _no_vendors(monkeypatch)
    approval = so.SpendApproval(max_cost=50, eligible=2, smartskip=True, directskip=True)
    try:
        so.run(_people(3), approval)
    except so.OrchestratorError as e:
        assert "covered 2" in str(e)
    else:
        raise AssertionError("approval for 2 records was accepted for 3")


def _token_env(monkeypatch):
    """Point the approval token at a temp file; returns (token path, export path)."""
    import tempfile
    d = tempfile.mkdtemp()
    monkeypatch.setattr(so, "_STATE_DIR", so.Path(d))
    monkeypatch.setattr(so, "APPROVAL_TOKEN_PATH", so.Path(d) / "approval_token.json")
    monkeypatch.setattr(so, "PENDING_PATH", so.Path(d) / "pending_approval.json")
    monkeypatch.setattr(so, "_is_interactive", lambda: False)
    monkeypatch.setattr(so, "_ask", _forbid)          # the chat path never prompts
    return so.APPROVAL_TOKEN_PATH, os.path.join(d, "export.csv")


def test_approval_refuses_when_no_terminal_and_no_chat_approval(monkeypatch):
    _, export = _token_env(monkeypatch)
    try:
        so.request_approval(_people(), 50, input_path=export)
    except so.OrchestratorError as e:
        assert "hasn't approved" in str(e) and "Nothing was spent" in str(e)
    else:
        raise AssertionError("approved with no terminal and no chat approval")


def test_chat_approval_token_accepted_once(monkeypatch):
    tok, export = _token_env(monkeypatch)
    so.write_approval_token(export, 50, 3, True, True)   # what the hook does
    a = so.request_approval(_people(3), 50, input_path=export)
    assert a == so.SpendApproval(max_cost=50.0, eligible=3, smartskip=True, directskip=True)
    assert not tok.exists()                               # consumed
    try:
        so.request_approval(_people(3), 50, input_path=export)
    except so.OrchestratorError:
        pass
    else:
        raise AssertionError("a chat approval was reused")


def test_chat_approval_token_must_match_the_exact_run(monkeypatch):
    import time as _time
    tok, export = _token_env(monkeypatch)
    cases = [
        ("different file", dict(input_path=export + ".other"), "different file"),
        ("different ceiling", dict(max_cost=60), "ceiling"),
        ("different batch", dict(n=2), "different batch"),
        ("different vendors", dict(run_smartskip=False), "different batch"),
    ]
    for label, kw, needle in cases:
        so.write_approval_token(export, 50, 3, True, True)
        try:
            so.request_approval(_people(kw.get("n", 3)), kw.get("max_cost", 50),
                                run_smartskip=kw.get("run_smartskip", True),
                                input_path=kw.get("input_path", export))
        except so.OrchestratorError as e:
            assert needle in str(e), (label, str(e))
            assert not tok.exists(), label               # a mismatch burns the token too
        else:
            raise AssertionError(f"accepted a token for a {label}")
    # expired
    so.write_approval_token(export, 50, 3, True, True)
    data = so.json.loads(tok.read_text())
    data["created"] = _time.time() - so.APPROVAL_TOKEN_TTL_S - 1
    tok.write_text(so.json.dumps(data))
    try:
        so.request_approval(_people(3), 50, input_path=export)
    except so.OrchestratorError as e:
        assert "expired" in str(e)
    else:
        raise AssertionError("accepted an expired token")


def test_approval_refuses_anything_but_the_exact_ceiling(monkeypatch):
    monkeypatch.setattr(so, "_is_interactive", lambda: True)
    for typed in ("y", "yes", "", "5", "50.01", "approve"):
        monkeypatch.setattr(so, "_ask", lambda prompt, t=typed: t)
        try:
            so.request_approval(_people(), 50)
        except so.OrchestratorError as e:
            assert "Nothing was spent" in str(e)
        else:
            raise AssertionError(f"approved on typed={typed!r}")


def test_approval_cancelled_by_eof_or_ctrl_c(monkeypatch):
    monkeypatch.setattr(so, "_is_interactive", lambda: True)
    for exc in (EOFError, KeyboardInterrupt):
        def boom(prompt, exc=exc):
            raise exc()
        monkeypatch.setattr(so, "_ask", boom)
        try:
            so.request_approval(_people(), 50)
        except so.OrchestratorError as e:
            assert "cancelled" in str(e)
        else:
            raise AssertionError(f"{exc.__name__} did not cancel")


def test_approval_accepts_typed_ceiling(monkeypatch):
    monkeypatch.setattr(so, "_is_interactive", lambda: True)
    for typed in ("50", "50.00", "$50", " 50.0 "):
        monkeypatch.setattr(so, "_ask", lambda prompt, t=typed: t)
        a = so.request_approval(_people(3), 50, run_smartskip=True, run_directskip=False)
        assert a == so.SpendApproval(max_cost=50.0, eligible=3, smartskip=True, directskip=False)


def test_approval_refuses_ceiling_below_skip_cost_before_prompting(monkeypatch):
    monkeypatch.setattr(so, "_is_interactive", lambda: True)
    monkeypatch.setattr(so, "_ask", _forbid)
    try:
        so.request_approval(_people(3), 0.50)   # 3 x (0.15 + 0.10) = 0.75 > 0.50
    except so.OrchestratorError as e:
        assert "exceeds --max-cost" in str(e)
    else:
        raise AssertionError("ceiling below skip cost was offered for approval")


def test_estimate_cli_writes_pending_and_yes_chain_approves_run(monkeypatch):
    """estimate -> pending; owner's yes (approve_pending) -> token; run's approval
    consumes it. Without the yes step, the same run refuses."""
    import io
    import contextlib
    _, export = _token_env(monkeypatch)
    with open(export, "w", encoding="utf-8", newline="") as fh:
        fh.write("Owner First Name,Owner Last Name,Property Street Address,Property City,"
                 "Property State,Property ZIP Code\n"
                 "Jane,Doe,1 A St,Austin,TX,78704\nBob,Roe,2 B St,Austin,TX,78704\n")
    monkeypatch.setattr(so.sys, "argv", ["x", "estimate", "--input", export])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert so.main() == 0
    assert "AWAITING OWNER APPROVAL" in buf.getvalue()
    pend = so.json.loads(so.PENDING_PATH.read_text())
    assert pend["eligible"] == 2 and pend["max_cost"] == 1.0   # ceil($0.92)

    notices = so.load_datasift_export(export)
    try:                                                         # no yes yet -> refuses
        so.request_approval(notices, 1.0, input_path=export)
    except so.OrchestratorError as e:
        assert "hasn't approved" in str(e)
    else:
        raise AssertionError("ran on a pending estimate with no approval")

    approved = so.approve_pending()                              # what the yes-hook does
    assert approved["max_cost"] == 1.0 and not so.PENDING_PATH.exists()
    a = so.request_approval(notices, 1.0, input_path=export)
    assert a.max_cost == 1.0 and a.eligible == 2


def test_approve_pending_refuses_stale_missing_or_underfunded(monkeypatch):
    import time as _time
    _token_env(monkeypatch)
    try:
        so.approve_pending()
    except so.OrchestratorError as e:
        assert "no pending" in str(e)
    else:
        raise AssertionError("approved with nothing pending")
    est = {"eligible": 100, "total_low": 30, "total_high": 40}
    so.write_pending_approval("e.csv", est, 40, True, True)
    try:
        so.approve_pending(5)                  # 100 x $0.25 skip cost > $5
    except so.OrchestratorError as e:
        assert "exceeds" in str(e)
    else:
        raise AssertionError("approved a ceiling below the skip cost")
    assert not so.APPROVAL_TOKEN_PATH.exists()
    data = so.json.loads(so.PENDING_PATH.read_text())
    data["created"] = _time.time() - so.PENDING_TTL_S - 1
    so.PENDING_PATH.write_text(so.json.dumps(data))
    try:
        so.approve_pending()
    except so.OrchestratorError as e:
        assert "expired" in str(e)
    else:
        raise AssertionError("approved an expired pending")


def test_smartskip_charge_counts_against_ceiling_even_if_order_fails(monkeypatch):
    """Card charged, then the order fails: DirectSkip + Trestle must NOT get the
    full ceiling again (this overran $0.80 -> $1.20 before the fix)."""
    import tempfile
    bill = {"ss": 0.0, "ds": 0.0, "trestle": 0.0}

    class FakeSS:
        def submit(self, p):
            return {"entities": 3, "bulkSkipId": "x"}

        def pay(self, bid, confirm_rows):
            bill["ss"] += confirm_rows * so.SS_COST
            return True

        def wait_for_completion(self, bid):
            raise ss.SmartSkipError("bulk skip ended in status failed")

    class FakeDS:
        n = 0

        def search_record(self, notice):
            FakeDS.n += 1
            bill["ds"] += so.DS_COST
            r = ds.DirectSkipRecord(first="Jane", last="Doe", property_address=notice.address,
                                    result_code="CI", source="api")
            r.subject_phones = [{"number": f"512555{FakeDS.n * 10 + j:04d}", "type": "mobile"}
                                for j in range(10)]
            return r

    def fake_score_all(merged, budget, **kw):
        nums = so._all_digits(merged)[:int(budget / so.TRESTLE_COST)]
        bill["trestle"] += len(nums) * so.TRESTLE_COST
        return {d: {"tier": "Dial First", "score": 90, "valid": True, "litigator": False}
                for d in nums}

    monkeypatch.setattr(so.smartskip, "SmartSkipClient", FakeSS)
    monkeypatch.setattr(so.smartskip, "build_trace_csv", lambda e, p: None)
    monkeypatch.setattr(so.directskip, "DirectSkipClient", FakeDS)
    monkeypatch.setattr(so, "score_all", fake_score_all)

    approval = so.SpendApproval(max_cost=0.80, eligible=3, smartskip=True, directskip=True)
    tmp = tempfile.mkdtemp()
    stats = so.run(_people(3), approval, out_path=os.path.join(tmp, "out.csv"), tmp_dir=tmp)
    real = round(sum(bill.values()), 2)
    assert real <= 0.80, f"real spend ${real} exceeded the $0.80 ceiling"
    assert stats["cost"] >= round(bill["ss"], 2)   # the failed order's charge is reported


def test_cli_run_stops_without_any_approval():
    """End-to-end, no patching: the real CLI run with piped stdin + captured
    stdout (how an agent's shell tool runs it) and no permission-prompt token
    must stop before any spend, even when the correct ceiling is piped in."""
    import subprocess
    export = _tmp("Owner First Name,Owner Last Name,Property Street Address,Property City,"
                  "Property State,Property ZIP Code\nJane,Doe,123 Main St,Austin,TX,78704\n")
    env = dict(os.environ, APIFY_TOKEN="", SMARTSKIP_EMAIL="", SMARTSKIP_PASSWORD="",
               DIRECTSKIP_API_KEY="", TRESTLE_API_KEY="", PYTHONIOENCODING="utf-8")
    script = os.path.join(os.path.dirname(__file__), "..", "src", "skip_orchestrator.py")
    out = _tmp("", ext="csv")
    os.remove(out)
    proc = subprocess.run([sys.executable, script, "run", "--input", export,
                           "--max-cost", "5", "--out", out],
                          input="5\n", capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "APPROVAL REQUIRED" in proc.stdout
    assert "Not approved" in proc.stderr and "Nothing was spent" in proc.stderr
    assert not os.path.exists(out)   # never reached the write step


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

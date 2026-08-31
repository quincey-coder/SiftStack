"""Austin BSC state round-trip + dedup-key tests (fake KVS, two simulated runs).

Run: PYTHONPATH=src python tests/test_bsc_state_roundtrip.py
"""
import os
import sys

os.environ["APIFY_IS_AT_HOME"] = "1"   # exercise the in-memory (KVS) path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import bsc_state as st  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


def _reset(fake_kvs: dict) -> dict:
    st._APIFY_STATE_CACHE = None
    st.inject_apify_state(fake_kvs.get(st.KVS_KEY))
    return st.load_state()


def test_escalation_and_seen_docs_roundtrip():
    fake_kvs: dict = {}

    # run 1: agenda 100 — CL X recommended repair
    state = _reset(fake_kvs)
    is_new, prior, eff = st.update_case(state, "CL 2026-000001", "2026-08-26", "repair",
                                        trv="", parcel="0123456789", address="1 Main St", zip5="78702")
    assert (is_new, prior, eff) == (True, "", "repair")
    diff = st.DiffResult(new=["CL 2026-000001"], meetings=["2026-08-26"])
    st.commit_run(state, diff, {"100": {"meeting": "2026-08-26"}})
    fake_kvs[st.KVS_KEY] = st.snapshot_apify_state()
    assert "100" in fake_kvs[st.KVS_KEY]["seen_docs"]
    assert fake_kvs[st.KVS_KEY]["cases"]["CL 2026-000001"]["order_class"] == "repair"

    # run 2: new process; agenda 100 already seen; agenda 101 escalates X to demolish
    state = _reset(fake_kvs)
    assert "100" in state["seen_docs"]
    is_new, prior, eff = st.update_case(state, "CL 2026-000001", "2026-09-23", "demolish")
    assert (is_new, prior, eff) == (False, "repair", "demolish")
    rec = state["cases"]["CL 2026-000001"]
    assert rec["class_history"] == [["2026-08-26", "repair"], ["2026-09-23", "demolish"]]
    assert rec["appearances"] == ["2026-08-26", "2026-09-23"]
    assert rec["parcel"] == "0123456789"          # carried forward

    # run 3: uphold-penalties return contributes nothing -> no escalation, no downgrade
    is_new, prior, eff = st.update_case(state, "CL 2026-000001", "2026-10-28",
                                        st.effective_class("uphold_penalties", "repair"))
    assert (is_new, prior, eff) == (False, "demolish", "demolish")
    assert st.effective_class("uphold_penalties") == ""
    assert st.effective_class("repair", "vacate") == "vacate"


def test_dedup_key_namespaced_by_case_and_class():
    from main import _notice_dedup_key
    a = NoticeData(notice_type="condemned", county="Travis", address="1 Main St", zip="78702",
                   parcel_id="0123456789", case_id="CL 2026-000001",
                   source_url="https://services.austintexas.gov/edims/document.cfm?id=479428#case=CL-2026-000001")
    a.order_class = "repair"
    b = NoticeData(notice_type="condemned", county="Travis", address="1 Main St", zip="78702",
                   parcel_id="0123456789", case_id="CL 2026-000001")
    b.order_class = "demolish"
    c = NoticeData(notice_type="condemned", county="Travis", address="1 Main St", zip="78702",
                   parcel_id="0123456789", case_id="cl 2026-000001")
    c.order_class = "repair"
    ka, kb, kc = _notice_dedup_key(a), _notice_dedup_key(b), _notice_dedup_key(c)
    assert ka == "case:condemned|travis|CL2026-000001|repair"
    assert kb == "case:condemned|travis|CL2026-000001|demolish"
    assert ka != kb and ka == kc
    # never collides with the parcel key another type would produce
    cv = NoticeData(notice_type="code_violation", county="Travis", parcel_id="0123456789",
                    source_url="https://data.austintexas.gov/x")
    assert _notice_dedup_key(cv) == "parcel:0123456789"
    assert not ka.startswith("parcel:")


def test_slack_summary_shapes():
    quiet = st.format_slack_summary(st.DiffResult())
    assert "quiet day" in quiet
    busy = st.format_slack_summary(st.DiffResult(
        new=["CL 1"], returning=["CL 2"], escalated=[["CL 2", "repair", "demolish"]],
        meetings=["2026-08-26"], skipped_appeals=2))
    assert "NEW cases: *1*" in busy and "CL 2: repair -> *demolish*" in busy
    alarm = st.format_slack_summary(st.DiffResult(parse_zero_docs=1))
    assert "rotating_light" in alarm


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
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

"""Per-PDF memory (pdf_notice_state) round-trips through a fake KVS and disk.

Run: PYTHONPATH=src python tests/test_pdf_notice_state_roundtrip.py   (or pytest)
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["APIFY_IS_AT_HOME"] = "1"   # exercise the in-memory (KVS) path first
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config  # noqa: E402
from notice_parser import NoticeData  # noqa: E402
from scrapers import pdf_notice_state as st  # noqa: E402


def _reset(fake_kvs: dict, county="Bell") -> dict:
    st._APIFY_STATE_CACHE.clear()
    st.inject_apify_state(county, fake_kvs.get(st.kvs_key(county)))
    return st.load_state(county)


def _notice(addr, owner="Ann Lee"):
    return NoticeData(
        notice_type="foreclosure", county="Bell", state="TX", address=addr, city="Killeen",
        zip="76541", owner_name=owner, tax_owner_name=owner, auction_date="2026-10-06",
        date_added="2026-09-09", source_url="http://x/a.pdf", raw_text="R" * 900,
    )


def test_kvs_roundtrip_replay_resume_and_invalidation():
    fake_kvs: dict = {}
    state = _reset(fake_kvs)
    sha_a, size_a = st.fingerprint(b"AAA")
    st.record_pdf(
        state, "http://x/a.pdf", sha256=sha_a, size=size_a, label="Sept 1", month="2026-09",
        parser_version=1, text_source="ocr", pages_total=2, pages_done=2, complete=True,
        partial_text="should be dropped", returned=2,
        notices=[_notice("1 Elm St"), _notice("2 Elm St")], today="2026-09-09",
    )
    sha_b, size_b = st.fingerprint(b"BBBB")
    st.record_pdf(
        state, "http://x/b.pdf", sha256=sha_b, size=size_b, label="Sept 2", month="2026-09",
        parser_version=1, text_source="ocr", pages_total=3, pages_done=1, complete=False,
        partial_text="PAGE1 TEXT", returned=0, notices=[], today="2026-09-09",
    )
    st.commit_run("Bell", state, {"pdfs_total": 2, "_private": 1}, today="2026-09-09")
    fake_kvs[st.kvs_key("Bell")] = st.snapshot_apify_state("Bell")

    # run 2
    state2 = _reset(fake_kvs)
    assert state2["last_run_date"] == "2026-09-09"
    assert state2["runs"][-1] == {"date": "2026-09-09", "pdfs_total": 2}
    a = state2["pdfs"]["http://x/a.pdf"]
    assert a["complete"] and a["partial_text"] == "" and len(a["records"][0]["raw_text"]) == 500
    assert st.is_unchanged(a, sha_a, size_a, 1)
    assert not st.is_unchanged(a, sha_a, size_a + 1, 1)     # size drift
    assert not st.is_unchanged(a, sha_a, size_a, 2)         # parser bump
    assert not st.is_unchanged(None, sha_a, size_a, 1)
    assert st.size_matches(a, size_a) and not st.size_matches(a, None)
    replayed = st.replay(a, "Bell")
    assert [n.address for n in replayed] == ["1 Elm St", "2 Elm St"]
    assert replayed[0].county == "Bell" and replayed[0].notice_type == "foreclosure"
    assert replayed[0].owner_name == "Ann Lee" and replayed[0].auction_date == "2026-10-06"
    b = state2["pdfs"]["http://x/b.pdf"]
    assert not b["complete"] and b["pages_done"] == 1 and b["partial_text"] == "PAGE1 TEXT"
    # first_seen survives a re-record
    st.record_pdf(
        state2, "http://x/b.pdf", sha256=sha_b, size=size_b, label="Sept 2", month="2026-09",
        parser_version=1, text_source="ocr", pages_total=3, pages_done=3, complete=True,
        partial_text="", returned=1, notices=[_notice("3 Elm St")], today="2026-09-10",
    )
    assert state2["pdfs"]["http://x/b.pdf"]["first_seen"] == "2026-09-09"
    assert state2["pdfs"]["http://x/b.pdf"]["last_seen"] == "2026-09-10"


def test_prune_and_runs_cap():
    fake_kvs: dict = {}
    state = _reset(fake_kvs, "Williamson")
    for i, month in enumerate(["2026-05", "2026-09", "2026-10"]):
        st.record_pdf(
            state, f"k{i}", sha256="s", size=1, label="", month=month, parser_version=1,
            text_source="none", pages_total=0, pages_done=0, complete=True, partial_text="",
            returned=0, notices=[], today="2026-05-01" if month == "2026-05" else "2026-09-09",
        )
    dropped = st.prune(state, {"2026-09", "2026-10"}, today=datetime(2026, 9, 9))
    assert dropped == 1 and set(state["pdfs"]) == {"k1", "k2"}
    # a recently seen entry outside keep_months survives
    st.record_pdf(
        state, "k9", sha256="s", size=1, label="", month="2026-08", parser_version=1,
        text_source="none", pages_total=0, pages_done=0, complete=True, partial_text="",
        returned=0, notices=[], today="2026-09-08",
    )
    assert st.prune(state, {"2026-09"}, today=datetime(2026, 9, 9)) == 0
    for i in range(70):
        st.commit_run("Williamson", state, {"i": i}, today=f"2026-01-{(i % 28) + 1:02d}")
    assert len(state["runs"]) == st.MAX_RUNS


def test_disk_roundtrip_and_touch():
    os.environ.pop("APIFY_IS_AT_HOME", None)
    # .env refills an UNSET APIFY_TOKEN, and any token reads as "on Apify" -> blank it.
    saved_token = os.environ.get("APIFY_TOKEN")
    os.environ["APIFY_TOKEN"] = ""
    saved_root = config.PROJECT_ROOT
    tmp = tempfile.mkdtemp()
    try:
        config.PROJECT_ROOT = Path(tmp)
        state = st.load_state("Bell")
        assert state["pdfs"] == {}
        st.record_pdf(
            state, "http://x/c.pdf", sha256="s", size=3, label="Oct 1", month="2026-10",
            parser_version=1, text_source="layer", pages_total=1, pages_done=1, complete=True,
            partial_text="", returned=1, notices=[_notice("5 Oak")], today="2026-09-09",
        )
        st.commit_run("Bell", state, {}, today="2026-09-09")
        assert st.state_path("Bell").exists()
        again = st.load_state("Bell")
        assert again["pdfs"]["http://x/c.pdf"]["records"][0]["address"] == "5 Oak"
        st.touch(again, "http://x/c.pdf", today="2026-09-10")
        assert again["pdfs"]["http://x/c.pdf"]["last_seen"] == "2026-09-10"
        # corrupt file -> empty
        st.state_path("Bell").write_text("{not json", encoding="utf-8")
        assert st.load_state("Bell")["pdfs"] == {}
    finally:
        config.PROJECT_ROOT = saved_root
        os.environ["APIFY_IS_AT_HOME"] = "1"
        if saved_token is None:
            os.environ.pop("APIFY_TOKEN", None)
        else:
            os.environ["APIFY_TOKEN"] = saved_token


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

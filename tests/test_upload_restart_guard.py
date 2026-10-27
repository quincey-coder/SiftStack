"""Restart guard for the Actor's wizard upload (offline — browser stubbed).

Apify migrates runs to a new host mid-flight and the Actor restarts from
scratch. ``upload_datasift_split(resume=…, on_progress=…)`` must then skip
the CSVs / bulk actions that already landed — but only when the regenerated
CSV is byte-identical, so a changed re-scrape can never lose a row.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import datasift_uploader as up  # noqa: E402
import upload_outcome as uo  # noqa: E402

TAG = "upload_2026-09-19"


class _Page:
    async def goto(self, *a, **k): pass
    async def wait_for_timeout(self, *a, **k): pass


class _Ctx:
    async def new_page(self): return _Page()


class _Browser:
    async def new_context(self, **k): return _Ctx()
    async def close(self): pass


class _Chromium:
    async def launch(self, **k): return _Browser()


class _PW:
    chromium = _Chromium()
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _stub(monkeypatch, *, fail_label=None, skip_ok=True):
    calls = {"upload": [], "enrich": 0, "skip": 0, "login": 0}

    async def _login(page, e, p):
        calls["login"] += 1
        return True

    async def _upload(page, path, list_name=None, existing_list=False):
        calls["upload"].append(list_name)
        if list_name == fail_label:
            return {"success": False, "message": "wizard step 4 stuck"}
        return {"success": True, "message": "ok"}

    async def _enrich(page, ln, tag=None):
        calls["enrich"] += 1
        return {"success": True, "message": "enrich submitted"}

    async def _skip(page, ln, tag=None):
        calls["skip"] += 1
        return {"success": skip_ok, "message": "skip submitted" if skip_ok else "no rows"}

    async def _noop(page): pass

    monkeypatch.setenv("DATASIFT_EMAIL", "x@y.z")
    monkeypatch.setenv("DATASIFT_PASSWORD", "pw")
    monkeypatch.setattr(up, "async_playwright", lambda: _PW())
    monkeypatch.setattr(up, "login", _login)
    monkeypatch.setattr(up, "upload_csv", _upload)
    monkeypatch.setattr(up, "enrich_records", _enrich)
    monkeypatch.setattr(up, "skip_trace_records", _skip)
    monkeypatch.setattr(up, "_dismiss_popups", _noop)
    return calls


def _infos(tmp_path, names=("Foreclosure", "Probate", "Liens")):
    infos = []
    for n in names:
        p = tmp_path / f"datasift_{n.lower()}.csv"
        p.write_text(f"Property Street Address,Tags\n1 Main St,{TAG} {n}\n", encoding="utf-8")
        infos.append({"path": p, "label": n, "list_name": n, "count": 1})
    return infos


def _run(infos, **kw):
    saved = []

    async def _progress(state):
        saved.append(state)

    res = asyncio.run(up.upload_datasift_split(
        # skip_trace is OFF by default since 2026-10-21 (owner decision); these
        # tests exercise the restart bookkeeping of BOTH bulk actions, so ask
        # for it explicitly.
        infos, existing_list=True, batch_tag=TAG, on_progress=_progress,
        **{"skip_trace": True, **kw}))
    return res, saved


def test_progress_is_reported_after_each_csv_and_bulk_action(tmp_path, monkeypatch):
    calls = _stub(monkeypatch)
    res, saved = _run(_infos(tmp_path))
    assert res["success"] and calls["upload"] == ["Foreclosure", "Probate", "Liens"]
    assert [len(s["uploaded"]) for s in saved] == [1, 2, 3, 3, 3]
    assert saved[-1]["enrich_done"] and saved[-1]["skip_trace_done"]


def test_restart_skips_identical_csvs_and_finishes_the_rest(tmp_path, monkeypatch):
    infos = _infos(tmp_path)
    calls = _stub(monkeypatch)
    resume = {"uploaded": {"Foreclosure": up._csv_sha256(infos[0]["path"])}}
    res, saved = _run(infos, resume=resume)
    assert calls["upload"] == ["Probate", "Liens"]
    assert res["success"] and len(res["uploads"]) == 3
    assert res["uploads"][0]["resumed"] is True
    assert calls["enrich"] == 1 and calls["skip"] == 1
    # index-aligned with csv_infos → the failure mapper sees nothing wrong
    assert uo.failures_from_playwright(res, infos, []) == []


def test_changed_csv_is_reuploaded_not_skipped(tmp_path, monkeypatch):
    infos = _infos(tmp_path)
    calls = _stub(monkeypatch)
    resume = {"uploaded": {"Foreclosure": "0" * 64}}  # hash from a different re-scrape
    _run(infos, resume=resume)
    assert calls["upload"] == ["Foreclosure", "Probate", "Liens"]


def test_fully_completed_run_never_opens_a_browser(tmp_path, monkeypatch):
    infos = _infos(tmp_path)
    calls = _stub(monkeypatch)
    resume = {"uploaded": {i["label"]: up._csv_sha256(i["path"]) for i in infos},
              "enrich_done": True, "skip_trace_done": True}
    res, saved = _run(infos, resume=resume)
    assert res["success"] and res["resumed"] is True
    assert calls == {"upload": [], "enrich": 0, "skip": 0, "login": 0}
    assert saved == []
    assert uo.failures_from_playwright(res, infos, []) == []


def test_restart_after_uploads_only_reruns_the_missing_bulk_action(tmp_path, monkeypatch):
    infos = _infos(tmp_path)
    calls = _stub(monkeypatch)
    resume = {"uploaded": {i["label"]: up._csv_sha256(i["path"]) for i in infos},
              "enrich_done": True}
    res, saved = _run(infos, resume=resume)
    assert calls["upload"] == [] and calls["enrich"] == 0 and calls["skip"] == 1
    assert res["enrich_result"]["resumed"] is True
    assert saved[-1]["skip_trace_done"] is True


def test_failed_bulk_action_is_not_marked_done(tmp_path, monkeypatch):
    calls = _stub(monkeypatch, skip_ok=False)
    res, saved = _run(_infos(tmp_path))
    assert saved[-1]["enrich_done"] is True and saved[-1]["skip_trace_done"] is False


def test_failed_csv_is_not_marked_uploaded(tmp_path, monkeypatch):
    calls = _stub(monkeypatch, fail_label="Probate")
    res, saved = _run(_infos(tmp_path))
    assert res["success"] is False
    assert list(saved[-1]["uploaded"]) == ["Foreclosure"]
    assert calls["enrich"] == 0 and calls["skip"] == 0


def test_unreadable_csv_is_never_treated_as_uploaded(tmp_path, monkeypatch):
    calls = _stub(monkeypatch)
    infos = [{"path": tmp_path / "missing.csv", "label": "Foreclosure",
              "list_name": "Foreclosure", "count": 1}]
    _run(infos, resume={"uploaded": {"Foreclosure": ""}})
    assert calls["upload"] == ["Foreclosure"]

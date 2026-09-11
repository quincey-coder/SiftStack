"""DataSift upload failures → per-list UploadFailure, Slack alert, manifest.

Run: PYTHONPATH=src python -m pytest tests/test_upload_outcome.py
"""

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import upload_outcome as uo  # noqa: E402

CSVS = [
    {"path": Path("output/datasift_foreclosure_2026-09-11.csv"), "label": "Foreclosure",
     "list_name": "Foreclosure", "count": 12},
    {"path": Path("output/datasift_probate_2026-09-11.csv"), "label": "Probate",
     "list_name": "Probate", "count": 8},
    {"path": Path("output/datasift_heirs_2026-09-11.csv"), "label": "Heirs",
     "list_name": "Heirs", "count": 3},
]
DRIVE = [
    {"label": "Foreclosure", "filename": "datasift_foreclosure_2026-09-11.csv",
     "url": "https://drive.example/fc"},
    {"label": "Probate", "filename": "datasift_probate_2026-09-11.csv",
     "url": "https://drive.example/pb"},
]


def test_split_stops_at_first_failure_and_marks_the_rest_not_attempted():
    result = {
        "success": False,
        "uploads": [
            {"success": True, "label": "Foreclosure", "message": "ok"},
            {"success": False, "label": "Probate",
             "message": "No clickable 'Next Step' on the map_columns screen"},
        ],
        "message": "Uploaded 2/3 CSVs",
    }
    fails = uo.failures_from_playwright(result, CSVS, DRIVE)
    assert [(f.label, f.stage) for f in fails] == [
        ("Probate", uo.STAGE_UPLOAD), ("Heirs", uo.STAGE_NOT_ATTEMPTED),
    ]
    probate, heirs = fails
    assert "map_columns" in probate.error
    assert probate.drive_url == "https://drive.example/pb"
    assert probate.records == 8 and probate.records_missing
    assert "stopped after the Probate failure" in heirs.error
    assert heirs.drive_url == ""  # heirs CSV never went to Drive in this fixture


def test_login_failure_loses_every_list():
    result = {"success": False, "message": "DataSift login failed", "uploads": []}
    fails = uo.failures_from_playwright(result, CSVS)
    assert [f.stage for f in fails] == [uo.STAGE_LOGIN] * 3
    assert all(f.records_missing for f in fails)


def test_single_csv_login_vs_upload_stage():
    one = CSVS[:1]
    login = uo.failures_from_playwright(
        {"success": False, "message": "DATASIFT_EMAIL and DATASIFT_PASSWORD must be set"}, one)
    assert login[0].stage == uo.STAGE_LOGIN
    wiz = uo.failures_from_playwright(
        {"success": False, "message": "Clicked 'Finish Upload' but the wizard is still open"}, one)
    assert wiz[0].stage == uo.STAGE_UPLOAD


def test_landed_but_bulk_actions_failed():
    result = {
        "success": True,
        "uploads": [{"success": True}, {"success": True}, {"success": True}],
        "enrich_result": {"success": False, "per_list": [
            {"list_name": "Foreclosure", "success": True},
            {"list_name": "Probate", "success": False,
             "message": "List filter for 'Probate' did not apply"},
            {"list_name": "Heirs", "success": True},
        ]},
        "skip_trace_result": {"success": False, "per_list": [
            {"list_name": "Foreclosure", "success": False, "message": "Duplicated task"},
            {"list_name": "Probate", "success": True},
            {"list_name": "Heirs", "success": True},
        ]},
    }
    fails = uo.failures_from_playwright(result, CSVS, DRIVE)
    assert {(f.label, f.stage) for f in fails} == {
        ("Probate", uo.STAGE_ENRICH), ("Foreclosure", uo.STAGE_SKIP_TRACE),
    }
    assert not any(f.records_missing for f in fails)


def test_all_good_yields_nothing():
    result = {"success": True, "uploads": [{"success": True}] * 3,
              "enrich_result": {"success": True}, "skip_trace_result": {"success": True}}
    assert uo.failures_from_playwright(result, CSVS, DRIVE) == []
    assert uo.build_slack_block([]) == ""
    assert uo.report_upload_failures([], out_dir=Path(".")) == {}


def test_api_results_with_fatal_and_skip_trace():
    results = [
        {"label": "Foreclosure", "list_name": "Foreclosure", "success": True,
         "skip_trace": {"success": False, "error": "HTTP 400: Insufficient balance!"}},
        {"label": "Probate", "list_name": "Probate", "success": False,
         "error": "HTTP 500: upstream"},
    ]
    fails = uo.failures_from_api(results, CSVS, DRIVE, fatal_error="crashed: KeyError: 'x'")
    assert [(f.label, f.stage) for f in fails] == [
        ("Foreclosure", uo.STAGE_SKIP_TRACE),
        ("Probate", uo.STAGE_UPLOAD),
        ("Heirs", uo.STAGE_CRASH),
    ]
    assert all(f.uploader == "api" for f in fails)
    auth = uo.failures_from_api([], CSVS, fatal_error="auth failed (HTTP 401): bad token")
    assert [f.stage for f in auth] == [uo.STAGE_LOGIN] * 3


def test_exception_marks_every_list_crashed():
    fails = uo.failures_from_exception(RuntimeError("browser died"), CSVS, uploader="playwright")
    assert [f.stage for f in fails] == [uo.STAGE_CRASH] * 3
    assert "RuntimeError: browser died" in fails[0].error


def test_slack_block_names_list_stage_action_and_csv():
    result = {"success": False, "uploads": [
        {"success": True}, {"success": False, "message": "wizard is still open"}]}
    fails = uo.failures_from_playwright(result, CSVS, DRIVE)
    block = uo.build_slack_block(fails, total_lists=3, manifest_ref="output/x.json")
    assert block.startswith(":rotating_light: *DataSift upload FAILED")
    assert "2 of 3 list(s) need attention (playwright) · 2 with records NOT in the CRM" in block
    assert '*Probate* → list "Probate" · 8 records — UPLOAD: wizard is still open' in block
    assert "↳ do: records are NOT in DataSift" in block
    assert "<https://drive.example/pb|datasift_probate_2026-09-11.csv>" in block
    assert '*Heirs* → list "Heirs" · 3 records — NOT ATTEMPTED' in block
    assert "↳ CSV: datasift_heirs_2026-09-11.csv" in block   # no Drive link → bare filename
    assert "Foreclosure" not in block.split("\n", 1)[1]      # landed lists are not listed
    assert "Manifest: output/x.json" in block
    assert ":warning:" not in block


def test_report_writes_manifest_logs_errors_and_sends_slack(tmp_path, monkeypatch, caplog):
    import slack_notifier

    sent = {}
    monkeypatch.setattr(slack_notifier, "_send_webhook",
                        lambda text, url=None: sent.setdefault("text", text) or True)
    fails = uo.failures_from_playwright(
        {"success": False, "message": "DataSift login failed", "uploads": []}, CSVS, DRIVE)
    with caplog.at_level(logging.ERROR, logger="upload_outcome"):
        manifest = uo.report_upload_failures(fails, out_dir=tmp_path, total_lists=3, run_id="run123")
    path = Path(manifest["manifest_path"])
    assert path.exists() and path.parent == tmp_path / "upload_failures"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["run_id"] == "run123"
    assert on_disk["lists_failed"] == 3 and on_disk["records_missing"] == 3
    assert on_disk["failures"][0]["stage"] == "login"
    assert on_disk["failures"][0]["action"].startswith("nothing landed")
    assert "3 of 3 list(s)" in sent["text"]
    assert f"Manifest: {path}" in sent["text"]
    errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert sum(1 for m in errs if m.startswith("DataSift upload failure [playwright/login]")) == 3
    # round-trips for the retry command
    assert [f.label for f in uo.load_manifest(path)] == ["Foreclosure", "Probate", "Heirs"]


def test_slack_off_still_writes_manifest(tmp_path, monkeypatch):
    import slack_notifier

    monkeypatch.setattr(slack_notifier, "_send_webhook",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")))
    fails = uo.failures_from_exception(RuntimeError("x"), CSVS, uploader="api")
    manifest = uo.report_upload_failures(fails, out_dir=tmp_path, slack=False)
    assert Path(manifest["manifest_path"]).exists()


def test_history_is_newest_first_and_capped():
    hist = []
    for i in range(35):
        hist = uo.append_history(hist, {"run_id": str(i)})
    assert len(hist) == uo.HISTORY_KEEP
    assert hist[0]["run_id"] == "34" and hist[-1]["run_id"] == "5"
    assert uo.append_history(None, {"run_id": "a"}) == [{"run_id": "a"}]
    assert uo.append_history("garbage", {"run_id": "a"}) == [{"run_id": "a"}]


def test_retry_only_reuploads_missing_lists(tmp_path, monkeypatch):
    """login/upload/not_attempted → re-upload; enrich-only → bulk action; landed lists untouched."""
    import asyncio

    import datasift_uploader

    csv_a = tmp_path / "datasift_probate.csv"
    csv_a.write_text("x")
    fails = [
        uo.UploadFailure("Probate", "Probate", uo.STAGE_UPLOAD, "playwright", "wizard", str(csv_a), 8),
        uo.UploadFailure("Foreclosure", "Foreclosure", uo.STAGE_ENRICH, "playwright", "filter", "", 12),
    ]
    manifest = uo.write_manifest(fails, tmp_path)

    calls = {}

    async def fake_split(csv_infos, enrich=True, skip_trace=True):
        calls["upload"] = [i["label"] for i in csv_infos]
        return {"success": True, "uploads": [{"success": True}] * len(csv_infos)}

    async def fake_bulk(list_names, *, enrich, skip_trace):
        calls["bulk"] = (list(list_names), enrich, skip_trace)
        return {"enrich": [{"list_name": ln, "success": True} for ln in list_names], "skip_trace": []}

    monkeypatch.setattr(datasift_uploader, "upload_datasift_split", fake_split)
    monkeypatch.setattr(uo, "_rerun_bulk_actions", fake_bulk)
    import config
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", "")

    assert uo.retry(str(manifest), slack=False) == 0
    assert calls["upload"] == ["Probate"]
    assert calls["bulk"] == (["Foreclosure"], True, False)
    asyncio.set_event_loop(asyncio.new_event_loop())  # asyncio.run left the loop closed on py<3.12


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

"""Actor auto-upload through the DataSift upload wizard (2026-09-17).

Covers the offline pieces of the port: the formatter's per-run batch tag,
the uploader's refusal to bulk-act on an established list without a tag
anchor, the tag-anchored enrich/skip-trace gate, the Slack block, and how
upload_outcome maps a tag-anchored bulk failure onto every list.

Run: PYTHONPATH=src python -m pytest tests/test_actor_datasift_upload.py
"""

import asyncio
import csv
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_formatter as fmt  # noqa: E402
import datasift_uploader as up  # noqa: E402
import slack_notifier as sn  # noqa: E402
import upload_outcome as uo  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


def _notice(**kw) -> NoticeData:
    n = NoticeData()
    n.address = "123 Main St"
    n.city = "Austin"
    n.state = "TX"
    n.zip = "78704"
    n.owner_name = "Jane Doe"
    n.notice_type = "foreclosure"
    n.county = "Travis"
    n.date_added = "2026-09-17"
    for k, v in kw.items():
        setattr(n, k, v)
    return n


# ── formatter: batch tag ────────────────────────────────────────────────

def test_batch_tag_lands_in_tags_of_lead_rows():
    tags = fmt._build_tags(_notice(), ["upload_2026-09-17"]).split(",")
    assert "upload_2026-09-17" in tags
    assert "Courthouse Data" in tags and "FTM" in tags


def test_batch_tag_not_duplicated_and_blank_ignored():
    tags = fmt._build_tags(_notice(), ["upload_2026-09-17", "upload_2026-09-17", "", None]).split(",")
    assert tags.count("upload_2026-09-17") == 1


def test_batch_tag_never_on_sold_or_resolved_cleanup_rows():
    sold = fmt._build_tags(_notice(record_status="sold"), ["upload_2026-09-17"])
    assert "upload_" not in sold
    resolved = fmt._build_tags(
        _notice(notice_type="code_violation", record_status="resolved"), ["upload_2026-09-17"],
    )
    assert "upload_" not in resolved


def test_write_by_notice_type_stamps_every_csv_including_heirs(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "OUTPUT_DIR", tmp_path)
    heir = _notice(
        address="9 Elm St", notice_type="probate", owner_deceased="yes",
        heir_map_json=json.dumps([{"name": "Kid Doe", "relationship": "child",
                                   "signing_authority": True, "status": "unverified"}]),
    )
    infos = fmt.write_datasift_by_notice_type([_notice(), heir], batch_tag="upload_2026-09-17")
    labels = [i["label"] for i in infos]
    assert "Heirs" in labels, labels
    for info in infos:
        with open(info["path"], newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows, info
        for row in rows:
            assert "upload_2026-09-17" in row["Tags"].split(","), (info["label"], row["Tags"])


def test_write_by_notice_type_without_batch_tag_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "OUTPUT_DIR", tmp_path)
    infos = fmt.write_datasift_by_notice_type([_notice()])
    with open(infos[0]["path"], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert not any(t.startswith("upload_") for t in rows[0]["Tags"].split(","))


# ── uploader: guards ─────────────────────────────────────────────────────

def test_split_refuses_bulk_actions_on_existing_lists_without_batch_tag(monkeypatch):
    monkeypatch.setenv("DATASIFT_EMAIL", "x@y.z")
    monkeypatch.setenv("DATASIFT_PASSWORD", "pw")
    infos = [{"path": Path("nope.csv"), "label": "Foreclosure", "list_name": "Foreclosure", "count": 1}]
    res = asyncio.run(up.upload_datasift_split(infos, existing_list=True, enrich=True, skip_trace=False))
    assert res["success"] is False
    assert "batch_tag" in res["message"]
    assert res["uploads"] == []


def test_enrich_records_aborts_when_tag_filter_does_not_apply(monkeypatch):
    calls = []

    async def _nav(page):
        calls.append("nav")

    async def _tag(page, tag):
        calls.append(("tag", tag))
        return False

    async def _boom(*a, **k):  # must never be reached
        raise AssertionError("select-all ran on an unfiltered page")

    monkeypatch.setattr(up, "_navigate_to_records", _nav)
    monkeypatch.setattr(up, "_filter_by_tag", _tag)
    monkeypatch.setattr(up, "_select_all_records", _boom)
    res = asyncio.run(up.enrich_records(object(), "Foreclosure", tag="upload_2026-09-17"))
    assert res["success"] is False
    assert "upload_2026-09-17" in res["message"] and "refusing" in res["message"]
    assert ("tag", "upload_2026-09-17") in calls


def test_skip_trace_records_uses_tag_anchor_not_list(monkeypatch):
    seen = {}

    async def _nav(page):
        pass

    async def _tag(page, tag):
        seen["tag"] = tag
        return False

    async def _list(page, name):
        seen["list"] = name
        return False

    monkeypatch.setattr(up, "_navigate_to_records", _nav)
    monkeypatch.setattr(up, "_filter_by_tag", _tag)
    monkeypatch.setattr(up, "_filter_by_list", _list)
    asyncio.run(up.skip_trace_records(object(), "Foreclosure", tag="upload_2026-09-17"))
    assert seen == {"tag": "upload_2026-09-17"}
    seen.clear()
    asyncio.run(up.skip_trace_records(object(), "Foreclosure"))
    assert seen == {"list": "Foreclosure"}


# ── Slack block + failure mapping ────────────────────────────────────────

CSVS = [
    {"path": Path("output/datasift_foreclosure.csv"), "label": "Foreclosure",
     "list_name": "Foreclosure", "count": 12},
    {"path": Path("output/datasift_probate.csv"), "label": "Probate",
     "list_name": "Probate", "count": 8},
    {"path": Path("output/datasift_heirs.csv"), "label": "Heirs",
     "list_name": "Heirs", "count": 3},
]


def test_slack_block_success_with_tag_and_bulk_status():
    result = {
        "success": True,
        "uploads": [{"success": True, "label": l["label"]} for l in CSVS],
        "message": "Uploaded 3/3 CSVs",
        "batch_tag": "upload_2026-09-17",
        "enrich_result": {"success": True, "message": "Enrichment started", "anchor": "tag:upload_2026-09-17"},
        "skip_trace_result": {"success": False, "message": "Could not find 'Send To' button"},
    }
    text = sn.build_datasift_upload_block(result, csv_infos=CSVS, notices=[_notice()])
    assert "DataSift upload (wizard)" in text
    assert "3/3" in text and "23 record(s)" in text
    assert "upload_2026-09-17" in text
    assert "Enrich: Enrichment started" in text
    assert "✗ Skip trace" in text
    assert "123 Main St" in text  # address sample from the notices


def test_slack_block_marks_not_attempted_after_a_failure():
    result = {
        "success": False,
        "uploads": [{"success": True, "label": "Foreclosure"},
                    {"success": False, "label": "Probate", "message": "wizard stuck on map_columns"}],
        "message": "Uploaded 2/3 CSVs",
        "batch_tag": "upload_2026-09-17",
    }
    text = sn.build_datasift_upload_block(result, csv_infos=CSVS)
    assert "1/3" in text
    assert "`Probate` — wizard stuck on map_columns" in text
    assert "`Heirs` — not attempted" in text and "Probate failure" in text


def test_slack_block_empty_when_no_upload_attempted():
    assert sn.build_datasift_upload_block({}, csv_infos=CSVS) == ""
    assert sn.build_datasift_upload_block(None, csv_infos=CSVS) == ""


def test_tag_anchored_bulk_failure_maps_to_every_list():
    result = {
        "success": True,
        "uploads": [{"success": True, "label": l["label"]} for l in CSVS],
        "message": "Uploaded 3/3 CSVs",
        "batch_tag": "upload_2026-09-17",
        "skip_trace_result": {"success": False, "anchor": "tag:upload_2026-09-17",
                              "message": "Filter for tag 'upload_2026-09-17' did not apply"},
    }
    fails = uo.failures_from_playwright(result, CSVS)
    assert sorted(f.list_name for f in fails) == ["Foreclosure", "Heirs", "Probate"]
    assert {f.stage for f in fails} == {uo.STAGE_SKIP_TRACE}
    assert not any(f.records_missing for f in fails)

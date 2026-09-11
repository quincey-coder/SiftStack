"""Warnings are log-only; only errors reach the Slack health block.

Run: PYTHONPATH=src python -m pytest tests/test_run_health_slack.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import run_health as rh  # noqa: E402


def _baseline(key, counts, zero_streak=0):
    return {key: {
        "history": [{"date": f"2026-09-0{i + 1}", "count": c, "status": "ok" if c else "zero"}
                    for i, c in enumerate(counts)],
        "zero_streak": zero_streak,
    }}


def _mixed_run():
    h = rh.RunHealth()
    h.record_scraper("Bell", "foreclosure", count=0, error="Cloudflare 403")           # ERROR
    h.record_scraper("Travis", "probate", count=0, evidence={"returned": 40})         # ERROR (parse-zero)
    h.record_scraper("Travis", "lien", count=0)                                        # WARN (zero-streak)
    h.record_scraper("Bell", "lien", count=900, evidence={"window_days": 1, "hit_cap": True})  # WARN
    h.record_scraper("Williamson", "probate", count=12)                                # ok
    h.record_event("warn", "list_validator", "passed with 7 warning(s)")
    h.record_event("error", "Travis guardrail", "APN format drift 60%")
    h.zillow = {"attempted": 50, "failed": 30, "enriched": 20}                        # WARN
    h.log_counts = {"warnings": 42, "errors": 2, "last_errors": ["x: boom"]}
    baselines = {}
    baselines.update(_baseline("Travis/lien", [5, 6, 4, 0], zero_streak=1))
    baselines.update(_baseline("Bell/lien", [30, 28, 31]))
    return rh.analyze(h, baselines)


def test_errors_and_warnings_are_separated():
    r = _mixed_run()
    assert any("Bell/foreclosure — FAIL" in f for f in r.flags)
    assert any("40 returned, 0 parsed" in f for f in r.flags)
    assert any("Travis guardrail" in e for e in r.event_lines)
    assert len(r.flags) == 2 and len(r.event_lines) == 1
    warn_text = "\n".join(r.warnings)
    assert "Travis/lien — 0 records 2 days running" in warn_text
    assert "Bell/lien — hit the source's result cap" in warn_text
    assert "list_validator: passed with 7 warning(s)" in warn_text
    assert "failure share high" in warn_text
    assert len(r.warnings) == 4


def test_slack_block_has_no_warning_lines():
    r = _mixed_run()
    block = rh.build_health_block(r)
    assert ":warning:" not in block
    assert "WARN" not in block                      # the old "Logs: N ERROR · N WARN" tail
    assert "days running" not in block
    assert "result cap" not in block
    assert "list_validator" not in block
    assert "failure share high" not in block
    # errors still there, unchanged
    assert "Bell/foreclosure — FAIL: Cloudflare 403" in block
    assert "40 returned, 0 parsed" in block
    assert "Travis guardrail: APN format drift 60%" in block
    assert "Logs: 2 ERROR" in block
    assert "↳ x: boom" in block
    # Zillow line stays plain/informational
    assert "Zillow: 20/50 enriched · 30 failed (60%)" in block
    # one quiet count line so the operator knows to look at the log
    assert "4 warnings in the run log (not shown here)" in block


def test_log_only_block_includes_warnings():
    r = _mixed_run()
    block = rh.build_health_block(r, include_warnings=True)
    assert "• warn: Travis/lien — 0 records 2 days running" in block
    assert "not shown here" not in block


def test_warnings_alone_keep_the_run_green():
    h = rh.RunHealth()
    h.record_scraper("Travis", "lien", count=0)
    h.record_event("warn", "list_validator", "passed with 3 warning(s)")
    h.zillow = {"attempted": 50, "failed": 30}
    h.log_counts = {"warnings": 9, "errors": 0, "last_errors": []}
    r = rh.analyze(h, _baseline("Travis/lien", [5, 6, 4, 0], zero_streak=1))
    assert r.warnings and r.healthy
    block = rh.build_health_block(r)
    assert block.startswith(":white_check_mark:")
    assert "Logs:" not in block                     # no ERROR count line when errors == 0
    assert rh.format_status_message(r).startswith("OK:")


def test_warnings_are_written_to_the_log(caplog):
    r = _mixed_run()
    with caplog.at_level(logging.WARNING, logger="run_health"):
        rh.log_warnings(r)
    msgs = [rec.getMessage() for rec in caplog.records]
    assert len(msgs) == 4
    assert all(m.startswith("health warn (log-only") for m in msgs)
    assert any("list_validator: passed with 7 warning(s)" in m for m in msgs)


def test_send_health_report_logs_warnings_and_sends_errors_only(monkeypatch, caplog):
    import slack_notifier

    sent = {}
    monkeypatch.setattr(slack_notifier, "_send_webhook", lambda text, url=None: sent.setdefault("text", text) or True)
    r = _mixed_run()
    with caplog.at_level(logging.WARNING, logger="run_health"):
        assert rh.send_health_report(r, "https://hooks.example/x")
    assert ":warning:" not in sent["text"]
    assert "Cloudflare 403" in sent["text"]
    assert sum(1 for rec in caplog.records if "health warn (log-only" in rec.getMessage()) == 4


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

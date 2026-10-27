"""Bell CAD portal migration (2026-10-27) regression tests.

Three things broke when Bell CAD moved from esearch.bellcad.org to the GSA
portal and stopped re-posting the delinquent XLSX:
  1. the appraisal-roll regex missed the renamed September export,
  2. every Bell tax-delinquent Source URL pointed at a dead host,
  3. a three-month-stale delinquent roll produced 0 NEW / 0 DROPPED daily
     with no alarm.
Offline: a generated XLSX stands in for the download; state/raw/report
writes go to tmp_path.
"""

import asyncio
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bell_tax_cache  # noqa: E402
import config  # noqa: E402
from scrapers import tax_delinquent_bell as B  # noqa: E402
from scrapers import tax_delinquent_state as state_mod  # noqa: E402

HEADER = ["prop_id", "prop_type", "state_cd", "state_cd_desc", "Base_Tax_Due",
          "years_owed", "situs_address", "situs_city", "situs_state", "situs_zip",
          "Ownr_Name", "Ownr_Addr", "legal_desc", "collections_only", "DataDate"]

ROWS = [
    [100046, "Real Property", "A1", "REAL-RES/SINGLE FAMILY", "5000.00", "2022-2025",
     "1508 N GRAY ST", "KILLEEN", "TX", "76541", "GOMEZ, JOSE",
     "1710 SHERMAN DR\nKILLEEN, TX  76543-3247", "NORTH SIDE ADDITION, BLOCK 005, LOT 7",
     False, datetime(2026, 7, 17, 15, 45)],
]


# ── 1. appraisal-roll discovery ───────────────────────────────────────
PORTAL_HTML = """
<a href="https://bellcad.org/wp-content/uploads/2019/06/BellCAD_Delinquent_Roll_Condensed_20260717.xlsx">x</a>
<a href="https://bellcad.org/wp-content/uploads/2025/07/2024_BellCAD_Appraisal_Data_Condensed_20250722.xlsx">x</a>
<a href="https://bellcad.org/wp-content/uploads/2026/04/2025_BellCAD_Appraisal_Data_Condensed_20260407.xlsx">x</a>
<a href="https://bellcad.org/wp-content/uploads/2026/09/BellCAD-Appraisal-Data-Condensed-2026-20260922.xlsx">x</a>
<a href="https://bellcad.org/wp-content/uploads/2026/09/BellCAD-Appraisal-Data-Condensed-2027-20260922.xlsx">x</a>
<a href="https://bellcad.org/wp-content/uploads/2019/06/BellCAD-Collections-Delinquent-Roll-20260727.rar">x</a>
"""


def test_appraisal_pattern_matches_both_naming_styles():
    pat = config.BELLCAD_APPRAISAL_ROLL_PATTERN
    assert re.search(pat, "2025_BellCAD_Appraisal_Data_Condensed_20260407.xlsx")
    assert re.search(pat, "BellCAD-Appraisal-Data-Condensed-2026-20260922.xlsx")
    assert not re.search(pat, "BellCAD_Delinquent_Roll_Condensed_20260717.xlsx")


def test_discover_latest_prefers_newest_date_then_highest_year(monkeypatch):
    monkeypatch.setattr(bell_tax_cache, "_fetch_portal_html", lambda: PORTAL_HTML)
    url = bell_tax_cache.discover_latest_url(config.BELLCAD_APPRAISAL_ROLL_PATTERN)
    assert url.endswith("BellCAD-Appraisal-Data-Condensed-2027-20260922.xlsx")
    url = bell_tax_cache.discover_latest_url(config.BELLCAD_DELINQUENT_ROLL_PATTERN)
    assert url.endswith("BellCAD_Delinquent_Roll_Condensed_20260717.xlsx")


def test_filename_datestamp_and_age():
    assert bell_tax_cache.filename_datestamp(
        "https://x/BellCAD_Delinquent_Roll_Condensed_20260717.xlsx") == "20260717"
    assert bell_tax_cache.filename_datestamp("foo.xlsx") == ""
    assert bell_tax_cache.roll_age_days("20260717", today=date(2026, 10, 27)) == 102
    assert bell_tax_cache.roll_age_days("", today=date(2026, 10, 27)) is None
    assert bell_tax_cache.roll_age_days("2026071", today=date(2026, 10, 27)) is None


def test_delinquent_meta_uses_memo_before_portal(tmp_path, monkeypatch):
    """No sidecar + URL already discovered this process -> no second portal GET."""
    monkeypatch.setattr(config, "BELL_TAX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(bell_tax_cache, "_fetch_portal_html", lambda: PORTAL_HTML)
    bell_tax_cache._DISCOVERED.clear()
    bell_tax_cache.discover_latest_url(config.BELLCAD_DELINQUENT_ROLL_PATTERN)
    calls = []
    monkeypatch.setattr(bell_tax_cache, "_fetch_portal_html", lambda: calls.append(1) or PORTAL_HTML)
    meta = bell_tax_cache.delinquent_meta()
    assert meta["datestamp"] == "20260717" and calls == []


def test_env_int_tolerates_garbage(monkeypatch):
    monkeypatch.setenv("SIFT_BELL_ROLL_STALE_DAYS", "45d")
    assert config._env_int("SIFT_BELL_ROLL_STALE_DAYS", 45) == 45
    monkeypatch.setenv("SIFT_BELL_ROLL_STALE_DAYS", " 60 ")
    assert config._env_int("SIFT_BELL_ROLL_STALE_DAYS", 45) == 60


def test_source_url_pads_float_ids():
    """openpyxl can hand a numeric prop_id back as 100046.0."""
    import bell_gsa
    assert bell_gsa.parcel_url(100046.0).endswith("/parcel/0000100046")


def test_meta_sidecar_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BELL_TAX_CACHE_DIR", str(tmp_path / "cache"))
    path = bell_tax_cache._cache_dir() / "BellCAD_Delinquent_Roll_Condensed_latest.xlsx"
    path.write_bytes(b"x")
    bell_tax_cache._write_meta(
        path, "https://bellcad.org/x/BellCAD_Delinquent_Roll_Condensed_20260717.xlsx")
    meta = bell_tax_cache.delinquent_meta(refresh_if_missing=False)
    assert meta["datestamp"] == "20260717"
    assert meta["url"].endswith("_20260717.xlsx")
    assert meta["downloaded_at"]


# ── 2 + 3. scraper: Source URL and the staleness alarm ───────────────
@pytest.fixture
def fixture_xlsx(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DataExport"
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    p = tmp_path / "bell.xlsx"
    wb.save(p)
    return p


def _run(scraper):
    return asyncio.run(scraper.scrape(mode="daily"))


def test_source_url_points_at_gsa_parcel_page(fixture_xlsx):
    s = B.BellTaxDelinquentScraper(fixture_xlsx=fixture_xlsx, skip_target_zip=True)
    notices = _run(s)
    assert len(notices) == 1
    n = notices[0]
    assert n.parcel_id == "100046"
    assert n.source_url == "https://belltx-search.gsacorp.io/parcel/0000100046"
    assert "esearch" not in n.source_url


def test_fixture_runs_skip_the_staleness_check(fixture_xlsx):
    s = B.BellTaxDelinquentScraper(fixture_xlsx=fixture_xlsx, skip_target_zip=True)
    _run(s)
    assert B.LAST_ALARMS == []
    assert "roll_age_days" not in s.last_meta


def test_stale_roll_raises_error_alarm(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "BELLCAD_DELINQUENT_STALE_DAYS", 45)
    monkeypatch.setattr(bell_tax_cache, "delinquent_meta", lambda **kw: {
        "url": "https://bellcad.org/x/BellCAD_Delinquent_Roll_Condensed_20260717.xlsx",
        "datestamp": "20260717", "downloaded_at": "",
    })
    monkeypatch.setattr(bell_tax_cache, "roll_age_days", lambda ds, today=None: 102)
    monkeypatch.setattr(state_mod, "load_state", lambda county: {
        "runs": [{"raw_sha256": "old"}] + [{"raw_sha256": "same"}] * 3,
    })
    s = B.BellTaxDelinquentScraper(skip_target_zip=True)
    B.LAST_ALARMS.clear()
    s._check_roll_staleness("same")
    assert len(B.LAST_ALARMS) == 1
    msg = B.LAST_ALARMS[0]
    assert "102 days old" in msg and "20260717" in msg
    assert "comparing the same file to itself for 3 runs" in msg
    assert "belltx-search.gsacorp.io/tax/" in msg
    assert s.last_meta["roll_unchanged_runs"] == 3
    assert s.last_meta["roll_age_days"] == 102


def test_fresh_roll_is_quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bell_tax_cache, "delinquent_meta", lambda **kw: {
        "url": "https://bellcad.org/x/BellCAD_Delinquent_Roll_Condensed_20261020.xlsx",
        "datestamp": "20261020", "downloaded_at": "",
    })
    monkeypatch.setattr(bell_tax_cache, "roll_age_days", lambda ds, today=None: 7)
    monkeypatch.setattr(state_mod, "load_state", lambda county: {"runs": []})
    s = B.BellTaxDelinquentScraper(skip_target_zip=True)
    B.LAST_ALARMS.clear()
    s._check_roll_staleness("new")
    assert B.LAST_ALARMS == []
    assert s.last_meta["roll_unchanged_runs"] == 0

"""Williamson tax-delinquent scraper — current-year bills are not delinquent.

The Wilco "Current Year and Prior Taxes Due" file carries every freshly-billed
current-year row from the October certification until the following February
(277K of 287K rows on 2026-10-26). Those rows must not create a parcel, count
as a delinquent year, or add to the balance owed. Offline: a tiny generated
XLSX stands in for the download; state/raw/report writes go to tmp_path.
"""

import asyncio
import os
import sys
import time
from datetime import date
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config  # noqa: E402
from scrapers import tax_delinquent_wilco as W  # noqa: E402

HEADER = ["Tax Year", "Quick Ref", "Legal Desc", "Situs Address", "Owner Name",
          "Addr1", "Addr2", "City", "State", "Zip", "Fee Balance"]


def _row(year, qr, owner, fee, situs="100 MAIN ST GEORGETOWN 78626"):   # real Wilco shape: no commas, no state
    return [year, qr, "LOT 1", situs, owner, "PO BOX 1", "", "GEORGETOWN", "TX", "78626", fee]


ROWS = [
    # R1: 2024 + 2025 delinquent, 2026 current bill → 2 yrs, $3,500 (not $7,500)
    _row(2024, "R100001", "DOE JOHN", 1500.0),
    _row(2025, "R100001", "DOE JOHN", 2000.0),
    # the current-year row carries the freshest owner — harvested, not counted
    _row(2026, "R100001", "DOE JOHN & DOE JANE", 4000.0),
    # R2: current-year bill ONLY → not delinquent, must not appear anywhere
    _row(2026, "R100002", "ROE JANE", 12000.0),
    # R3: 2023..2025 + 2026 → 3 yrs, $3,500
    _row(2023, "R100003", "SMITH ANN", 1000.0),
    _row(2024, "R100003", "SMITH ANN", 1000.0),
    _row(2025, "R100003", "SMITH ANN", 1500.0),
    _row(2026, "R100003", "SMITH ANN", 5000.0),
    # R4: 2025 + 2026 → 1 yr delinquent (was "2" under the old rule)
    _row(2025, "R100004", "LEE SAM", 5000.0),
    _row(2026, "R100004", "LEE SAM", 5000.0),
    # Personal property → never a parcel
    _row(2024, "P200001", "ACME LLC", 9000.0),
]


@pytest.fixture
def fixture_xlsx(tmp_path, monkeypatch):
    from scrapers import tax_delinquent_state as st
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)   # state/raw/reports → tmp
    # .env on the operator box carries APIFY_TOKEN, which flips the state module
    # into its in-memory Apify mode and bypasses tmp_path — force local mode.
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("APIFY_IS_AT_HOME", raising=False)
    monkeypatch.setattr(st, "_is_apify", lambda: False)
    monkeypatch.setattr(W, "latest_delinquent_tax_year", lambda today=None: 2025)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DataExport"
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    path = tmp_path / "wilco.xlsx"
    wb.save(path)
    return path


def _scrape(path, **kw):
    kw.setdefault("skip_target_zip", True)
    return asyncio.run(W.WilliamsonTaxDelinquentScraper(fixture_xlsx=path, **kw).scrape())


@pytest.mark.parametrize("today,expected", [
    (date(2026, 10, 27), 2025),  # Oct: 2026 bills out, 2025 is the latest delinquent
    (date(2027, 1, 15), 2025),   # Jan: 2026 still merely due until Feb 1
    (date(2027, 2, 1), 2026),    # Feb 1: 2026 becomes delinquent
    (date(2026, 6, 30), 2025),
])
def test_latest_delinquent_tax_year(today, expected):
    assert W.latest_delinquent_tax_year(today) == expected


def test_current_year_rows_never_count(fixture_xlsx):
    notices = _scrape(fixture_xlsx, min_years=0, min_amount=0)
    by_id = {n.parcel_id: n for n in notices}

    assert set(by_id) == {"R100001", "R100003", "R100004"}, "R2 (2026-only) must not exist"
    assert by_id["R100001"].tax_delinquent_years == "2"
    assert by_id["R100001"].tax_delinquent_amount == "3500.00"   # excludes the $4,000 2026 bill
    assert by_id["R100001"].tax_owner_name == "DOE JOHN & DOE JANE"   # harvested from the 2026 row
    assert by_id["R100001"].address == "100 Main St"
    assert by_id["R100001"].city == "Georgetown"
    assert by_id["R100001"].zip == "78626"
    assert by_id["R100003"].tax_delinquent_years == "3"
    assert by_id["R100003"].tax_delinquent_amount == "3500.00"
    assert by_id["R100004"].tax_delinquent_years == "1"          # old rule said 2
    assert by_id["R100004"].tax_delinquent_amount == "5000.00"

    removed = W.LAST_RUN_REMOVED
    assert removed["current_year_rows"] == 4
    assert removed["non_real"] == 1


def test_default_buy_box_drops_one_year_parcel(fixture_xlsx):
    notices = _scrape(fixture_xlsx)   # defaults: 2+ yrs, $3,000+
    assert {n.parcel_id for n in notices} == {"R100001", "R100003"}
    assert W.LAST_RUN_REMOVED["under_years"] == 1   # R4


def test_roll_set_excludes_current_year_only_parcels(fixture_xlsx):
    """The cross-run diff baseline must be the DELINQUENT parcel set, so the
    October roll certification doesn't balloon it (and February collapse it)."""
    from scrapers import tax_delinquent_state as st
    _scrape(fixture_xlsx)
    state = st.load_state("Williamson")
    assert state["last_run_apns"] == ["R100001", "R100003", "R100004"]


def test_download_failure_raises_instead_of_returning_zero(monkeypatch):
    """A refused download must be a hard FAIL in run-health, never "0 notices"
    (it read as a quiet zero for 21 days when Cloudflare started 403-ing the
    Apify IP on 2026-10-06)."""
    import wilco_tax_cache
    from scrapers import ScraperError
    monkeypatch.setattr(wilco_tax_cache, "download_delinquent_xlsx", lambda force=False: None)
    with pytest.raises(ScraperError, match="download failed"):
        asyncio.run(W.WilliamsonTaxDelinquentScraper().scrape())


def test_fetch_falls_back_to_the_residential_proxy_on_403(monkeypatch):
    import requests
    import wilco_tax_cache as C
    calls = []

    class _Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = "ok"
        def raise_for_status(self):
            if self.status_code >= 400:
                err = requests.HTTPError(f"{self.status_code} Client Error")
                err.response = self
                raise err

    def fake_get(url, proxies=None, **kw):
        calls.append(proxies)
        return _Resp(200 if proxies else 403)

    monkeypatch.setattr(C.requests, "get", fake_get)
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    with pytest.raises(requests.HTTPError):          # no proxy configured → raise
        C._get("https://www.wilcotx.gov/x")
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://u:p@proxy.apify.com:8000")
    calls.clear()
    assert C._get("https://www.wilcotx.gov/x").status_code == 200
    assert calls == [None, {"http": "http://u:p@proxy.apify.com:8000",
                            "https": "http://u:p@proxy.apify.com:8000"}]


def test_empty_workbook_raises(tmp_path, monkeypatch):
    from scrapers import ScraperError
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    wb = openpyxl.Workbook(); wb.save(tmp_path / "empty.xlsx")   # one blank sheet, no rows
    with pytest.raises(ScraperError, match="no data rows"):
        asyncio.run(W.WilliamsonTaxDelinquentScraper(fixture_xlsx=tmp_path / "empty.xlsx").scrape())


def test_stale_cache_is_not_reused_after_a_failed_download(tmp_path, monkeypatch):
    """A cached roll ≤ 7 days old covers a transient fetch failure; older than
    that the downloader returns None so the scraper hard-fails instead of
    re-processing weeks-old data as today's run."""
    import wilco_tax_cache as C
    monkeypatch.setattr(config, "WILCO_TAX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "WILCO_TAX_CACHE_TTL_HOURS", 0)        # always re-download
    monkeypatch.setattr(C, "_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")))
    dest = tmp_path / "wilco_delinquent_latest.xlsx"; dest.write_bytes(b"x")
    fresh = time.time() - 2 * 3600
    os.utime(dest, (fresh, fresh))
    assert C.download_delinquent_xlsx() == dest
    stale = time.time() - (C.MAX_STALE_FALLBACK_HOURS + 1) * 3600
    os.utime(dest, (stale, stale))
    assert C.download_delinquent_xlsx() is None

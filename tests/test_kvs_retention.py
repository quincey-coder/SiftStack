"""State-store retention sweep (2026-09-17): keep newest 7 raw rolls per county,
drop deep-prospecting PDFs older than 30 days, touch nothing else."""

import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import kvs_retention as kr  # noqa: E402

KEYS = (
    [f"bell_tax_raw_2026-08-{d:02d}_111000_bell_delinquent.xlsx" for d in range(1, 11)]
    + [f"travis_tax_raw_2026-09-{d:02d}_110100_TaxDelqOpenData.csv" for d in range(10, 18)]
    + ["williamson_tax_raw_2026-09-17_110000_wilco.xlsx"]
    + ["100_bowie_cir_2026-06-09.pdf", "3_n_19th_st_2026-04-28.pdf", "1804_lennox_ave_2026-09-01.pdf"]
    + ["seen_notice_ids", "bell_texdel_state", "last_run_date", "scraper_health_state",
       "travis_texdel_2026-08-05_travis_texdel_diff.json", "datasift_foreclosure.csv",
       "bell_foreclosure_state", "output.csv"]
)


def test_keeps_newest_seven_per_county_and_old_pdfs_only():
    plan = kr.plan_deletions(KEYS, today=date(2026, 9, 17))
    assert plan["raw_rolls"] == sorted(
        [f"bell_tax_raw_2026-08-{d:02d}_111000_bell_delinquent.xlsx" for d in range(1, 4)]
        + ["travis_tax_raw_2026-09-10_110100_TaxDelqOpenData.csv"]
    )
    assert plan["pdfs"] == ["100_bowie_cir_2026-06-09.pdf", "3_n_19th_st_2026-04-28.pdf"]


def test_state_diff_and_csv_keys_are_never_planned():
    plan = kr.plan_deletions(KEYS, today=date(2026, 9, 17))
    doomed = set(plan["raw_rolls"]) | set(plan["pdfs"])
    for k in ("seen_notice_ids", "bell_texdel_state", "last_run_date", "scraper_health_state",
              "travis_texdel_2026-08-05_travis_texdel_diff.json", "datasift_foreclosure.csv",
              "bell_foreclosure_state", "output.csv"):
        assert k not in doomed


def test_sweep_deletes_planned_keys_and_survives_a_failure():
    class Store:
        def __init__(self):
            self.deleted = []

        async def list_keys(self, limit=1000, exclusive_start_key=None):
            return {"items": [{"key": k} for k in KEYS], "isTruncated": False}

        async def delete_record(self, key):
            if key.endswith("2026-08-01_111000_bell_delinquent.xlsx"):
                raise RuntimeError("boom")
            self.deleted.append(key)

    st = Store()
    res = asyncio.run(kr.sweep(st))
    assert res["scanned"] == len(KEYS)
    assert res["failed"] == 1
    assert res["deleted"] == len(res["planned"]["raw_rolls"]) + len(res["planned"]["pdfs"]) - 1
    assert "seen_notice_ids" not in st.deleted


def test_dry_run_deletes_nothing():
    class Store:
        async def list_keys(self, limit=1000, exclusive_start_key=None):
            return {"items": [{"key": k} for k in KEYS], "isTruncated": False}

        async def delete_record(self, key):
            raise AssertionError("dry run must not delete")

    res = asyncio.run(kr.sweep(Store(), dry_run=True))
    assert res["deleted"] == 0 and res["planned"]["raw_rolls"]

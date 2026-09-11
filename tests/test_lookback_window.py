"""Rolling lookback window + tccsearch lag + publicsearch sanity cap.

Run: PYTHONPATH=src python tests/test_lookback_window.py   (or pytest)
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import lookback  # noqa: E402
from scrapers.lookback import daily_window, since_iso  # noqa: E402
from scrapers.publicsearch_common import sanity_cap_for  # noqa: E402
from scrapers.tccsearch_common import effective_from_date  # noqa: E402

NOW = datetime(2026, 9, 9, 11, 0, 0)


def _clear_env():
    os.environ.pop(lookback.ENV_LOOKBACK, None)
    os.environ.pop("TCC_INDEX_LAG_DAYS", None)


def test_daily_widens_recent_since_to_14_days():
    _clear_env()
    frm, to, days = daily_window("daily", "2026-09-08", now=NOW)
    assert frm == NOW - timedelta(days=14), frm
    assert to == NOW and days == 14


def test_daily_keeps_older_explicit_since():
    _clear_env()
    frm, _, days = daily_window("daily", "2026-07-11", now=NOW)
    assert frm == datetime(2026, 7, 11), frm
    assert days == 60


def test_daily_no_since_uses_default_then_widens_no_further():
    _clear_env()
    frm, _, _ = daily_window("daily", None, now=NOW)
    assert frm == NOW - timedelta(days=30), frm   # 30 > 14, stays 30


def test_daily_invalid_since_falls_back_to_default():
    _clear_env()
    frm, _, _ = daily_window("daily", "not-a-date", now=NOW, default_days=5)
    assert frm == NOW - timedelta(days=14), frm   # 5-day default widened to 14


def test_env_override_widens():
    _clear_env()
    os.environ[lookback.ENV_LOOKBACK] = "30"
    try:
        frm, _, _ = daily_window("daily", "2026-09-08", now=NOW)
        assert frm == NOW - timedelta(days=30), frm
    finally:
        _clear_env()


def test_env_garbage_ignored():
    _clear_env()
    os.environ[lookback.ENV_LOOKBACK] = "-3"
    try:
        assert lookback.daily_lookback_days() == 14
    finally:
        _clear_env()


def test_historical_untouched():
    _clear_env()
    frm, _, days = daily_window("historical", "2026-09-08", now=NOW)
    assert frm == NOW - timedelta(days=365) and days == 365


def test_since_iso_format():
    _clear_env()
    assert since_iso("daily", "2026-09-08", now=NOW) == "2026-08-26T00:00:00"


def test_effective_from_date_defaults_to_lookback():
    _clear_env()
    out = effective_from_date(NOW - timedelta(days=1), NOW, "daily")
    assert out == NOW - timedelta(days=14), out


def test_effective_from_date_env_cannot_narrow():
    _clear_env()
    os.environ["TCC_INDEX_LAG_DAYS"] = "7"
    try:
        out = effective_from_date(NOW - timedelta(days=1), NOW, "daily")
        assert out == NOW - timedelta(days=14), out
    finally:
        _clear_env()


def test_effective_from_date_env_widens():
    _clear_env()
    os.environ["TCC_INDEX_LAG_DAYS"] = "60"
    try:
        out = effective_from_date(NOW - timedelta(days=1), NOW, "daily")
        assert out == NOW - timedelta(days=60), out
    finally:
        _clear_env()


def test_effective_from_date_historical_passthrough():
    _clear_env()
    frm = NOW - timedelta(days=365)
    assert effective_from_date(frm, NOW, "historical") == frm


def test_sanity_cap_table():
    assert sanity_cap_for(1) == 500
    assert sanity_cap_for(7) == 500
    assert sanity_cap_for(8) == 1000
    assert sanity_cap_for(14) == 1000
    assert sanity_cap_for(15) == 1500
    assert sanity_cap_for(28) == 2000
    assert sanity_cap_for(31) == 2500
    assert sanity_cap_for(45) is None
    assert sanity_cap_for(14, base=100) == 200


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

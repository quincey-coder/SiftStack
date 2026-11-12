"""Two CSVs that target the SAME DataSift list must both be uploaded.

2026-11-12 (run SVzz7yneq1ksLWijD, the first unattended cloud upload): the
county split produced a Bell Foreclosure CSV and a Travis Foreclosure CSV —
both labelled "Foreclosure". The restart guard kept its "already uploaded"
book by LABEL, so once the Bell file landed the Travis file was skipped as
"already uploaded earlier in this run (before a host restart)" although no
restart had happened, and the run still reported "Uploaded 3/3 CSVs".

These tests replay that run and attack the guard from every side: no restart,
a restart at every possible point, changed re-scrapes, corrupt / old-format
progress, and a randomized crash simulation against a fake DataSift.
Offline — the browser and the wizard are stubbed.
"""

import asyncio
import json
import logging
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import datasift_uploader as up  # noqa: E402
import upload_outcome as uo  # noqa: E402

TAG = "upload_2026-11-12"
HEADER = "Property Street Address,Property City,Tags\n"


# ── stubs ────────────────────────────────────────────────────────────


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


class HostDied(BaseException):
    """The Apify host went away mid-run (not an Exception: nothing may catch it)."""


class FakeDataSift:
    """Stands in for the wizard. Records every file that 'landed' as
    (list_name, file bytes) and can fail a file or kill the host on cue."""

    def __init__(self):
        self.landed: list[tuple[str, bytes]] = []
        self.calls: list[tuple[str, str]] = []      # (list_name, file name)
        self.enrich = 0
        self.skip = 0
        self.logins = 0
        self.fail_files: dict[str, int] = {}        # file name -> times to fail
        self.die_before_call: int | None = None     # host dies before the Nth wizard pass
        self.die_after_call: int | None = None      # ...or right after it landed (before progress is saved)

    def install(self, monkeypatch):
        ds = self

        async def _login(page, e, p):
            ds.logins += 1
            return True

        async def _upload(page, path, list_name=None, existing_list=False):
            n = len(ds.calls) + 1
            if ds.die_before_call == n:
                raise HostDied()
            name = Path(path).name
            ds.calls.append((list_name, name))
            if not Path(path).exists():
                return {"success": False, "message": "CSV file not found"}
            if ds.fail_files.get(name, 0) > 0:
                ds.fail_files[name] -= 1
                return {"success": False, "message": "Finish not accepted"}
            ds.landed.append((list_name, Path(path).read_bytes()))
            if ds.die_after_call == n:
                raise HostDied()
            return {"success": True, "message": "All set!"}

        async def _enrich(page, ln, tag=None):
            ds.enrich += 1
            return {"success": True, "message": "enrich submitted"}

        async def _skip(page, ln, tag=None):
            ds.skip += 1
            return {"success": True, "message": "skip submitted"}

        async def _noop(page): pass

        monkeypatch.setenv("DATASIFT_EMAIL", "x@y.z")
        monkeypatch.setenv("DATASIFT_PASSWORD", "pw")
        monkeypatch.setattr(up, "async_playwright", lambda: _PW())
        monkeypatch.setattr(up, "login", _login)
        monkeypatch.setattr(up, "upload_csv", _upload)
        monkeypatch.setattr(up, "enrich_records", _enrich)
        monkeypatch.setattr(up, "skip_trace_records", _skip)
        monkeypatch.setattr(up, "_dismiss_popups", _noop)
        return ds


class RunStore:
    """The run's key-value store: what survives a host restart. Values go
    through JSON exactly like Apify's store, so a non-serialisable state or a
    type that does not survive the round trip shows up here."""

    def __init__(self):
        self.raw: str | None = None
        self.saves = 0

    async def save(self, state):
        self.raw = json.dumps(state)
        self.saves += 1

    def load(self):
        return None if self.raw is None else json.loads(self.raw)


def _write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(HEADER + "".join(f"{r},Austin,{TAG}\n" for r in rows), encoding="utf-8")
    return p


def _info(tmp_path, fname, list_name, rows, county=""):
    return {"path": _write(tmp_path, fname, rows), "label": list_name,
            "list_name": list_name, "count": len(rows), "county": county}


def _nov12(tmp_path, stamp="122007"):
    """The three CSVs of the 2026-11-12 run (same labels, same row counts)."""
    return [
        _info(tmp_path, f"datasift_bell_foreclosure_2026-11-12_{stamp}.csv", "Foreclosure",
              ["210 Margarita Dr", "3201 Cottonwood Ln", "2027 Herald Dr", "2110 Clairidge Ave"], "Bell"),
        _info(tmp_path, f"datasift_travis_foreclosure_2026-11-12_{stamp}.csv", "Foreclosure",
              ["3501 Neal St", "6309 Emerald Forest Dr", "935 E 51st St", "508 Kemp St",
               "12409 Waterford Run Way", "1908 Hearthstone Dr", "5500 China Berry Rd",
               "8523 Honeysuckle Trl", "14311 Cummins Way"], "Travis"),
        _info(tmp_path, f"datasift_travis_fire_damage_2026-11-12_{stamp}.csv", "Fire Damage",
              ["2621 Jones Rd", "1624 Barton Springs Rd", "203 Bethany Creek Dr"], "Travis"),
    ]


def _attempt(infos, store=None, **kw):
    """One Actor attempt: read the saved progress, upload, save progress."""
    store = store if store is not None else RunStore()
    args = {"existing_list": True, "batch_tag": TAG, "enrich": False, "skip_trace": False,
            "resume": store.load(), "on_progress": store.save}
    args.update(kw)
    return asyncio.run(up.upload_datasift_split(infos, **args))


def _files(ds):
    return [name for _ln, name in ds.calls]


def _assert_all_landed(ds, infos):
    """Every CSV's exact bytes reached its own list at least once."""
    for info in infos:
        want = (info["list_name"], Path(info["path"]).read_bytes())
        assert want in ds.landed, f"{Path(info['path']).name} never reached list {info['list_name']!r}"


# ── the 2026-11-12 bug ───────────────────────────────────────────────


def test_nov12_two_foreclosure_csvs_are_both_uploaded(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    res = _attempt(infos)
    assert res["success"] is True
    assert _files(ds) == [Path(i["path"]).name for i in infos]
    _assert_all_landed(ds, infos)
    assert not any(u.get("resumed") for u in res["uploads"])
    assert uo.failures_from_playwright(res, infos, []) == []
    assert res["message"] == "Uploaded 3/3 CSVs"
    assert res["counts"] == {"csvs": 3, "uploaded": 3, "resumed": 0, "empty": 0,
                             "failed": 0, "not_attempted": 0}


def test_nov12_saved_progress_names_each_file_separately(tmp_path, monkeypatch):
    FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    store = RunStore()
    _attempt(infos, store)
    saved = store.load()
    assert saved["version"] == up.UPLOAD_PROGRESS_VERSION and saved["batch_tag"] == TAG
    assert [(e["list_name"], e["file"], e["rows"]) for e in saved["uploaded"]] == [
        ("Foreclosure", Path(infos[0]["path"]).name, 4),
        ("Foreclosure", Path(infos[1]["path"]).name, 9),
        ("Fire Damage", Path(infos[2]["path"]).name, 3),
    ]
    # each entry holds the hash of ITS OWN file (the old book stored the Travis
    # hash for the Bell upload)
    assert [e["sha256"] for e in saved["uploaded"]] == [up._csv_sha256(i["path"]) for i in infos]
    assert len({e["sha256"] for e in saved["uploaded"]}) == 3


def test_the_real_progress_record_of_the_nov12_run_is_not_trusted(tmp_path, monkeypatch):
    """The record run SVzz7yneq1ksLWijD left behind (old label-keyed format,
    wrong file's hash). If it ever reaches this code, every CSV is sent."""
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    old = {"uploaded": {"Foreclosure": up._csv_sha256(infos[1]["path"]),
                        "Fire Damage": up._csv_sha256(infos[2]["path"])},
           "enrich_done": False, "skip_trace_done": False, "batch_tag": TAG}
    res = _attempt(infos, resume=old)
    assert res["success"] and len(ds.calls) == 3
    _assert_all_landed(ds, infos)
    assert not any(u.get("resumed") for u in res["uploads"])


# ── a real restart, at every possible point ──────────────────────────


def _dirs(tmp_path, n=2):
    out = []
    for i in range(1, n + 1):
        d = tmp_path / f"a{i}"
        d.mkdir()
        out.append(d)
    return out


@pytest.mark.parametrize("die_before", [1, 2, 3])
def test_restart_before_each_file_sends_exactly_the_missing_ones(tmp_path, monkeypatch, die_before):
    ds = FakeDataSift().install(monkeypatch)
    store = RunStore()
    a1, a2 = _dirs(tmp_path)
    ds.die_before_call = die_before
    with pytest.raises(HostDied):
        _attempt(_nov12(a1, "120007"), store)
    ds.die_before_call = None
    # the restarted Actor re-scrapes: same rows, NEW file names
    infos = _nov12(a2, "122007")
    res = _attempt(infos, store)
    assert res["success"]
    _assert_all_landed(ds, infos)
    assert len(ds.landed) == 3, "a file was sent twice or not at all"
    done_before = die_before - 1
    assert [bool(u.get("resumed")) for u in res["uploads"]] == [i < done_before for i in range(3)]
    assert res["counts"]["uploaded"] == 3 - done_before
    assert res["counts"]["resumed"] == done_before
    assert uo.failures_from_playwright(res, infos, []) == []
    if done_before:
        assert f"{done_before} already uploaded before a host restart" in res["message"]
        assert res["uploads"][0]["message"].endswith("as datasift_bell_foreclosure_2026-11-12_120007.csv")


@pytest.mark.parametrize("die_after", [1, 2, 3])
def test_host_dying_before_progress_is_saved_resends_that_file(tmp_path, monkeypatch, die_after):
    """Landed in DataSift but the host died before the book was written: the
    restart cannot know, so it sends the file again (DataSift merges by
    address). Never the other way round."""
    ds = FakeDataSift().install(monkeypatch)
    store = RunStore()
    a1, a2 = _dirs(tmp_path)
    ds.die_after_call = die_after
    with pytest.raises(HostDied):
        _attempt(_nov12(a1), store)
    ds.die_after_call = None
    infos = _nov12(a2)
    res = _attempt(infos, store)
    assert res["success"]
    _assert_all_landed(ds, infos)
    assert len(ds.landed) == 4            # exactly one file went twice
    assert res["counts"]["resumed"] == die_after - 1


def test_three_restarts_in_a_row_still_send_each_file_once(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    store = RunStore()
    dirs = _dirs(tmp_path, 4)
    for n in range(3):                         # dies before file 1, then before 2, then before 3
        ds.die_before_call = len(ds.calls) + 1
        with pytest.raises(HostDied):
            _attempt(_nov12(dirs[n], f"12000{n}"), store)
        ds.die_before_call = None
        if n < 2:                              # let exactly one more file land, then die again
            ds.die_before_call = len(ds.calls) + 2
            with pytest.raises(HostDied):
                _attempt(_nov12(dirs[n], f"12100{n}"), store)
    ds.die_before_call = None
    infos = _nov12(dirs[3])
    res = _attempt(infos, store)
    assert res["success"]
    _assert_all_landed(ds, infos)
    assert len(ds.landed) == 3


def test_restart_with_a_changed_file_resends_only_that_file(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    store = RunStore()
    a1, a2 = _dirs(tmp_path)
    ds.die_before_call = 3                     # both Foreclosure files landed
    with pytest.raises(HostDied):
        _attempt(_nov12(a1), store)
    ds.die_before_call = None
    infos = _nov12(a2)
    # the re-scrape found one more Travis foreclosure
    with open(infos[1]["path"], "a", encoding="utf-8") as f:
        f.write(f"77 New Filing Rd,Austin,{TAG}\n")
    res = _attempt(infos, store)
    assert res["success"]
    _assert_all_landed(ds, infos)
    assert [bool(u.get("resumed")) for u in res["uploads"]] == [True, False, False]


def test_restart_with_the_files_in_another_order(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    store = RunStore()
    a1, a2 = _dirs(tmp_path)
    ds.die_before_call = 2                     # only Bell Foreclosure landed
    with pytest.raises(HostDied):
        _attempt(_nov12(a1), store)
    ds.die_before_call = None
    infos = list(reversed(_nov12(a2)))         # Fire Damage, Travis FC, Bell FC
    res = _attempt(infos, store)
    assert res["success"]
    _assert_all_landed(ds, infos)
    assert len(ds.landed) == 3
    assert [bool(u.get("resumed")) for u in res["uploads"]] == [False, False, True]


# ── same list, same content, and other identity traps ────────────────


def test_same_rows_for_two_different_lists_are_two_uploads(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    store = RunStore()
    rows = ["1 Shared St", "2 Shared St"]

    def build(d):
        return [_info(d, "datasift_liens.csv", "Liens", rows),
                _info(d, "datasift_lis_pendens.csv", "Lis Pendens", rows)]

    a1, a2 = _dirs(tmp_path)
    ds.die_before_call = 2
    with pytest.raises(HostDied):
        _attempt(build(a1), store)
    ds.die_before_call = None
    infos = build(a2)
    assert Path(infos[0]["path"]).read_bytes() == Path(infos[1]["path"]).read_bytes()
    res = _attempt(infos, store)
    assert res["success"]
    assert [ln for ln, _ in ds.landed] == ["Liens", "Lis Pendens"]
    assert [bool(u.get("resumed")) for u in res["uploads"]] == [True, False]


def test_identical_file_listed_twice_is_sent_twice_without_a_restart(tmp_path, monkeypatch):
    """Nothing sent during a call may ever make the call skip a file."""
    ds = FakeDataSift().install(monkeypatch)
    rows = ["1 Twin St"]
    infos = [_info(tmp_path, "a.csv", "Probate", rows), _info(tmp_path, "b.csv", "Probate", rows)]
    res = _attempt(infos)
    assert res["success"] and len(ds.landed) == 2
    assert res["counts"]["uploaded"] == 2 and res["counts"]["resumed"] == 0


def test_many_counties_and_types_all_go_through(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = []
    for ln in ["Foreclosure", "Probate", "Liens", "Lis Pendens", "Tax Delinquent"]:
        for county in ["Bell", "Travis", "Williamson"]:
            slug = ln.lower().replace(" ", "_")
            infos.append(_info(tmp_path, f"datasift_{county.lower()}_{slug}.csv", ln,
                               [f"{n} {county} {ln} Rd" for n in range(1, 4)], county))
    infos.append(_info(tmp_path, "datasift_heirs.csv", "Heirs", ["9 Heir St"]))
    res = _attempt(infos)
    assert res["success"] and res["message"] == "Uploaded 16/16 CSVs"
    assert _files(ds) == [Path(i["path"]).name for i in infos]
    _assert_all_landed(ds, infos)


# ── bad / foreign progress records ───────────────────────────────────


@pytest.mark.parametrize("bad", [
    "garbage", 42, ["Foreclosure"], {"uploaded": "Foreclosure"},
    {"version": 2, "uploaded": {"Foreclosure": "abc"}},
    {"version": 2, "uploaded": "all"},
    {"version": 2, "uploaded": [None, 7, "x", {}, {"list_name": "Foreclosure"},
                                {"list_name": "Foreclosure", "sha256": "nothex"},
                                {"list_name": "", "sha256": "0" * 64},
                                {"list_name": ["Foreclosure"], "sha256": "0" * 64}]},
    {"version": 1, "uploaded": []},
    {"version": "2", "uploaded": []},
    {"version": 3, "uploaded": []},
])
def test_unusable_progress_never_skips_and_never_crashes(tmp_path, monkeypatch, bad):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    res = _attempt(infos, resume=bad)
    assert res["success"] and len(ds.calls) == 3
    _assert_all_landed(ds, infos)


@pytest.mark.parametrize("version", [None, 1, 3, "2", 2.5])
def test_well_formed_record_of_another_version_is_ignored(tmp_path, monkeypatch, version):
    """Entries that look valid still do not count unless the record says it
    is the current format — a later format may mean something else by them."""
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    store = RunStore()
    _attempt(infos, store)
    state = store.load()
    if version is None:
        del state["version"]
    else:
        state["version"] = version
    res = _attempt(infos, resume=state)
    assert res["success"] and len(ds.calls) == 6
    assert res["counts"]["resumed"] == 0


def test_progress_from_another_batch_tag_is_ignored(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    store = RunStore()
    _attempt(infos, store)                     # everything recorded under TAG
    res = _attempt(infos, store, batch_tag="upload_2026-11-13")
    assert res["success"] and len(ds.calls) == 6


def test_truthy_strings_do_not_count_as_done_flags(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    store = RunStore()
    _attempt(infos, store)
    state = store.load()
    state["enrich_done"] = "false"
    state["skip_trace_done"] = 1
    res = _attempt(infos, enrich=True, skip_trace=True, resume=state)
    assert res["success"] and ds.enrich == 1 and ds.skip == 1


# ── bulk actions after a restart ─────────────────────────────────────


def test_new_rows_after_a_restart_get_the_bulk_actions_again(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    store = RunStore()
    infos = _nov12(tmp_path)
    _attempt(infos, store, enrich=True, skip_trace=True)
    assert (ds.enrich, ds.skip) == (1, 1)
    # identical restart: nothing sent, nothing re-run, no browser
    res = _attempt(infos, store, enrich=True, skip_trace=True)
    assert res["success"] and len(ds.calls) == 3 and (ds.enrich, ds.skip) == (1, 1) and ds.logins == 1
    # restart where one file gained a row: that file is sent, bulk actions run again
    with open(infos[2]["path"], "a", encoding="utf-8") as f:
        f.write(f"5 Late Fire Ln,Austin,{TAG}\n")
    res = _attempt(infos, store, enrich=True, skip_trace=True)
    assert res["success"] and len(ds.calls) == 4 and (ds.enrich, ds.skip) == (2, 2)
    assert store.load()["enrich_done"] is True and store.load()["skip_trace_done"] is True


# ── empty / unreadable / changing files ──────────────────────────────


def test_empty_csv_is_not_sent_and_does_not_block_the_others(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    infos.insert(1, _info(tmp_path, "datasift_probate.csv", "Probate", []))
    res = _attempt(infos)
    assert res["success"]
    assert _files(ds) == [Path(i["path"]).name for i in infos if i["count"]]
    assert res["uploads"][1]["skipped_empty"] is True
    assert res["message"] == "Uploaded 3/4 CSVs (1 empty, nothing to send)"
    assert uo.failures_from_playwright(res, infos, []) == []


def test_only_empty_csvs_means_no_login_and_no_bulk_action(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = [_info(tmp_path, "a.csv", "Probate", []), _info(tmp_path, "b.csv", "Liens", [])]
    res = _attempt(infos, enrich=True, skip_trace=True)
    assert res["success"] and ds.logins == 0 and ds.enrich == 0 and ds.skip == 0
    assert res["counts"]["empty"] == 2 and res["counts"]["uploaded"] == 0


def test_row_counter_never_calls_a_file_with_data_empty(tmp_path):
    cases = {
        "one_row.csv": HEADER + "1 Main St,Austin,t\n",
        "no_trailing_newline.csv": HEADER + "1 Main St,Austin,t",
        "bom.csv": "﻿" + HEADER + "1 Main St,Austin,t\n",
        "crlf.csv": HEADER.replace("\n", "\r\n") + "1 Main St,Austin,t\r\n",
        "multiline_notes.csv": 'Property Street Address,Notes\n"1 Main St","line one\nline two\n\nline four"\n',
        "blank_lines_between.csv": HEADER + "\n\n1 Main St,Austin,t\n\n",
    }
    for name, text in cases.items():
        p = tmp_path / name
        p.write_bytes(text.encode("utf-8"))
        assert up._csv_data_rows(p) == 1, name
    for name, text in {"header_only.csv": HEADER, "zero_bytes.csv": "", "blank.csv": "\n\n",
                       "header_and_commas.csv": HEADER + ",,\n"}.items():
        p = tmp_path / name
        p.write_bytes(text.encode("utf-8"))
        assert up._csv_data_rows(p) == 0, name
    # unreadable / unparseable -> None (or the true count), never 0: the
    # uploader only skips a file whose count is exactly 0
    assert up._csv_data_rows(tmp_path / "missing.csv") is None
    big = tmp_path / "huge_field.csv"
    big.write_text('Property Street Address,Notes\n"1 Main St","' + "x" * 200_000 + '"\n', encoding="utf-8")
    assert up._csv_data_rows(big) in (1, None)
    latin = tmp_path / "latin1.csv"
    latin.write_bytes(HEADER.encode() + "1 Pe\xf1a St,Austin,t\n".encode("latin-1"))
    assert up._csv_data_rows(latin) in (1, None)


def test_file_that_cannot_be_parsed_is_still_sent(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    latin = tmp_path / "latin1.csv"
    latin.write_bytes(HEADER.encode() + "1 Pe\xf1a St,Austin,t\n".encode("latin-1"))
    infos = [{"path": latin, "label": "Probate", "list_name": "Probate"}]
    res = _attempt(infos)
    assert res["success"] and len(ds.landed) == 1


def test_missing_file_is_a_reported_failure_not_a_silent_skip(tmp_path, monkeypatch):
    FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    Path(infos[1]["path"]).unlink()
    res = _attempt(infos)
    assert res["success"] is False
    fails = uo.failures_from_playwright(res, infos, [])
    assert [(f.stage, Path(f.csv_path).name) for f in fails] == [
        (uo.STAGE_UPLOAD, Path(infos[1]["path"]).name),
        (uo.STAGE_NOT_ATTEMPTED, Path(infos[2]["path"]).name),
    ]
    assert res["message"] == "Uploaded 1/3 CSVs (1 failed, 1 not attempted)"


def test_file_changed_while_uploading_is_not_recorded_as_done(tmp_path, monkeypatch):
    FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    store = RunStore()
    real_upload = up.upload_csv

    async def _mutating(page, path, list_name=None, existing_list=False):
        r = await real_upload(page, path, list_name=list_name, existing_list=existing_list)
        if "travis_foreclosure" in Path(path).name:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"1 Late Row Rd,Austin,{TAG}\n")
        return r

    monkeypatch.setattr(up, "upload_csv", _mutating)
    res = _attempt(infos, store)
    assert res["success"]
    assert [e["file"] for e in store.load()["uploaded"]] == [
        Path(infos[0]["path"]).name, Path(infos[2]["path"]).name]
    # a restart therefore sends the changed file again
    res = _attempt(infos, store)
    assert [bool(u.get("resumed")) for u in res["uploads"]] == [True, False, True]


def test_progress_store_failure_does_not_stop_the_upload(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)

    async def _broken(state):
        raise RuntimeError("key-value store unavailable")

    res = _attempt(infos, on_progress=_broken)
    assert res["success"] and len(ds.landed) == 3


def test_retry_then_success_on_the_second_same_list_file(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    ds.fail_files[Path(infos[1]["path"]).name] = 1
    res = _attempt(infos)
    assert res["success"] and len(ds.landed) == 3
    assert res["uploads"][1]["retried"] == 1
    assert res["counts"]["uploaded"] == 3


def test_failure_on_second_same_list_file_names_the_right_csv(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    ds.fail_files[Path(infos[1]["path"]).name] = 5
    links = [{"label": i["label"], "filename": Path(i["path"]).name,
              "url": f"https://drive.example/{Path(i['path']).name}"} for i in infos]
    res = _attempt(infos)
    assert res["success"] is False
    fails = uo.failures_from_playwright(res, infos, links)
    assert [Path(f.csv_path).name for f in fails] == [Path(infos[1]["path"]).name,
                                                      Path(infos[2]["path"]).name]
    # the Drive link is the failed file's own, not the Bell file with the same label
    assert fails[0].drive_url.endswith(Path(infos[1]["path"]).name)
    assert fails[0].records == 9


def _crashing_wizard(monkeypatch, crash_on: str, times: int):
    """Make the wizard RAISE (page-load timeout) for one file, `times` times."""
    real_upload = up.upload_csv
    left = {"n": times}

    async def _upload(page, path, list_name=None, existing_list=False):
        if crash_on in Path(path).name and left["n"] > 0:
            left["n"] -= 1
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded")
        return await real_upload(page, path, list_name=list_name, existing_list=existing_list)

    monkeypatch.setattr(up, "upload_csv", _upload)


def test_wizard_exception_is_retried_like_any_failed_pass(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    _crashing_wizard(monkeypatch, "travis_foreclosure", times=1)
    res = _attempt(infos)
    assert res["success"] and len(ds.landed) == 3
    _assert_all_landed(ds, infos)
    assert res["uploads"][1]["retried"] == 1
    assert "wizard crashed: TimeoutError" in res["uploads"][1]["first_failure"]


def test_wizard_exception_twice_blames_only_that_file(tmp_path, monkeypatch):
    """A crash used to escape the uploader: every file — also the ones that
    had landed — was then reported as lost, and nothing was retried."""
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    store = RunStore()
    _crashing_wizard(monkeypatch, "travis_foreclosure", times=2)
    res = _attempt(infos, store)
    assert res["success"] is False and len(ds.landed) == 1
    fails = uo.failures_from_playwright(res, infos, [])
    assert [(f.stage, Path(f.csv_path).name) for f in fails] == [
        (uo.STAGE_UPLOAD, Path(infos[1]["path"]).name),
        (uo.STAGE_NOT_ATTEMPTED, Path(infos[2]["path"]).name),
    ]
    # the Bell file is on record, so re-running the run sends only the other two
    assert [e["file"] for e in store.load()["uploaded"]] == [Path(infos[0]["path"]).name]
    ds.install(monkeypatch)                    # the wizard works again
    res = _attempt(infos, store)
    assert res["success"] and len(ds.landed) == 3
    _assert_all_landed(ds, infos)


def test_failed_page_reset_between_files_does_not_abort_the_batch(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)

    async def _broken_goto(self, *a, **k):
        raise TimeoutError("Page.goto: Timeout 30000ms exceeded")

    monkeypatch.setattr(_Page, "goto", _broken_goto)
    res = _attempt(infos)
    assert res["success"] and len(ds.landed) == 3
    _assert_all_landed(ds, infos)


def test_bulk_action_crash_is_not_reported_as_lost_uploads(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)

    async def _boom(page, ln, tag=None):
        raise TimeoutError("Manage menu never opened")

    monkeypatch.setattr(up, "enrich_records", _boom)
    res = _attempt(infos, enrich=True)
    assert len(ds.landed) == 3 and res["counts"]["uploaded"] == 3
    assert res["enrich_result"]["success"] is False
    fails = uo.failures_from_playwright(res, infos, [])
    assert fails and {f.stage for f in fails} == {uo.STAGE_ENRICH}
    assert not any(f.records_missing for f in fails)


def test_skip_decision_is_rechecked_when_the_files_turn_comes(tmp_path, monkeypatch):
    """After a restart the Fire Damage file is on record. While this call
    uploads the first file, the Fire Damage file changes on disk — it must be
    sent, not skipped on the strength of the earlier look."""
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    store = RunStore()
    _attempt([infos[2]], store)                # Fire Damage landed in an earlier attempt
    real_upload = up.upload_csv

    async def _upload(page, path, list_name=None, existing_list=False):
        r = await real_upload(page, path, list_name=list_name, existing_list=existing_list)
        if "bell_foreclosure" in Path(path).name:
            with open(infos[2]["path"], "a", encoding="utf-8") as f:
                f.write(f"99 Changed Meanwhile Dr,Austin,{TAG}\n")
        return r

    monkeypatch.setattr(up, "upload_csv", _upload)
    res = _attempt(infos, store)
    assert res["success"]
    _assert_all_landed(ds, infos)              # the CHANGED Fire Damage bytes are in
    assert not res["uploads"][2].get("resumed")


def test_empty_decision_is_rechecked_when_the_files_turn_comes(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    late = _info(tmp_path, "datasift_probate.csv", "Probate", [])
    infos.append(late)
    real_upload = up.upload_csv

    async def _upload(page, path, list_name=None, existing_list=False):
        r = await real_upload(page, path, list_name=list_name, existing_list=existing_list)
        if "bell_foreclosure" in Path(path).name:
            with open(late["path"], "a", encoding="utf-8") as f:
                f.write(f"12 Late Probate Ct,Austin,{TAG}\n")
        return r

    monkeypatch.setattr(up, "upload_csv", _upload)
    res = _attempt(infos)
    assert res["success"] and len(ds.landed) == 4
    assert ("Probate", Path(late["path"]).read_bytes()) in ds.landed


def test_drive_link_is_never_borrowed_from_a_same_label_file():
    info = {"path": "out/datasift_travis_foreclosure_x.csv", "label": "Foreclosure"}
    bell_only = [{"label": "Foreclosure", "filename": "datasift_bell_foreclosure_x.csv",
                  "url": "https://drive.example/bell"}]
    assert uo._drive_url_for(info, bell_only) == ""
    assert uo._drive_url_for(info, [{"label": "Foreclosure", "url": "https://drive.example/only"}]) \
        == "https://drive.example/only"


# ── what Slack says ──────────────────────────────────────────────────


def test_slack_block_tells_the_two_foreclosure_files_apart(tmp_path, monkeypatch):
    import slack_notifier as sn
    FakeDataSift().install(monkeypatch)
    store = RunStore()
    infos = _nov12(tmp_path)
    links = [{"label": i["label"], "filename": Path(i["path"]).name,
              "url": f"https://drive.example/{i['county']}/{i['label'].replace(' ', '')}"} for i in infos]
    res = _attempt(infos, store)
    block = sn.build_datasift_upload_block(res, csv_infos=infos, drive_links=links)
    assert "3/3 list(s) succeeded · 16 record(s) total" in block
    assert "Foreclosure (Bell)>* — 4 record(s)" in block
    assert "Foreclosure (Travis)>* — 9 record(s)" in block
    assert "https://drive.example/Bell/Foreclosure" in block
    assert "https://drive.example/Travis/Foreclosure" in block
    assert "not sent again" not in block
    # after a restart the block says which files were not sent again
    res = _attempt(infos, store)
    block = sn.build_datasift_upload_block(res, csv_infos=infos, drive_links=links)
    assert block.count("not sent again — this exact file already landed") == 3
    assert "3 file(s) not sent again" in block


# ── randomized crash simulation ──────────────────────────────────────


def _simulate(seed, root, ds):
    rng = random.Random(seed)
    lists = rng.sample(["Foreclosure", "Probate", "Liens", "Fire Damage", "Tax Delinquent"],
                       k=rng.randint(1, 3))
    specs = []                                  # [list_name, rows]
    for j in range(rng.randint(1, 8)):
        roll = rng.random()
        if specs and roll < 0.25:               # byte-identical to an earlier CSV
            rows = list(rng.choice(specs)[1])
        elif roll < 0.33:
            rows = []                           # header-only file
        else:
            rows = [f"{rng.randint(1, 9999)} Sim St {seed}-{j}-{k}" for k in range(rng.randint(1, 4))]
        specs.append([rng.choice(lists), rows])
    store = RunStore()
    crashes_left = rng.randint(0, 4)
    clean = crashes_left == 0
    for attempt in range(1, 40):
        d = root / f"s{seed}_a{attempt}"
        d.mkdir(parents=True)
        if attempt > 1:                         # the restarted Actor re-scrapes
            for s in specs:
                if rng.random() < 0.15:
                    s[1] = s[1] + [f"late row {seed}-{attempt}-{rng.randint(1, 999)}"]
            if rng.random() < 0.2:
                rng.shuffle(specs)
        infos = [_info(d, f"datasift_{j}_{attempt}.csv", ln, rows)
                 for j, (ln, rows) in enumerate(specs)]
        landed_before = list(ds.landed)
        calls_before = len(ds.calls)
        ds.die_before_call = ds.die_after_call = None
        ds.fail_files = {}
        if crashes_left:
            point = len(ds.calls) + rng.randint(1, len(specs))
            if rng.random() < 0.5:
                ds.die_before_call = point
            else:
                ds.die_after_call = point
        if rng.random() < 0.25:                 # a wizard pass that fails once (retried) or for good
            clean = False
            victim = rng.choice(infos)
            ds.fail_files[Path(victim["path"]).name] = rng.choice([1, 1, 5])
        try:
            res = _attempt(infos, store)
        except HostDied:
            crashes_left -= 1
            continue
        # 1. nothing is ever skipped unless that exact content already reached that list
        for info, u in zip(infos, res["uploads"]):
            if u.get("resumed"):
                assert (info["list_name"], Path(info["path"]).read_bytes()) in landed_before, \
                    f"seed {seed}: {info['label']} skipped but never uploaded"
            if u.get("skipped_empty"):
                assert info["count"] == 0, f"seed {seed}: non-empty file called empty"
        c = res["counts"]
        assert c["uploaded"] + c["resumed"] + c["empty"] + c["failed"] + c["not_attempted"] == len(infos)
        if not res["success"]:
            assert uo.failures_from_playwright(res, infos, []), f"seed {seed}: silent failure"
            continue                            # operator re-runs / resurrects the run
        # 2. success means every non-empty CSV is in its list, byte for byte
        for info in infos:
            if info["count"]:
                assert (info["list_name"], Path(info["path"]).read_bytes()) in ds.landed, \
                    f"seed {seed}: {Path(info['path']).name} missing from {info['list_name']}"
        assert c["failed"] == 0 and c["not_attempted"] == 0
        assert uo.failures_from_playwright(res, infos, []) == []
        # 3. a run with no restart and no failure sends every file exactly once, in order
        if clean and attempt == 1:
            sent = [name for _ln, name in ds.calls[calls_before:]]
            assert sent == [Path(i["path"]).name for i in infos if i["count"]], f"seed {seed}"
            assert c["resumed"] == 0
        return attempt
    raise AssertionError(f"seed {seed}: never finished")


def test_randomized_restarts_never_lose_a_file(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    seeds = int(os.environ.get("UPLOAD_SIM_SEEDS", "150"))   # raise it for a deeper soak
    attempts = [_simulate(seed, tmp_path, ds) for seed in range(seeds)]
    assert max(attempts) > 1 and min(attempts) == 1   # the simulation really did restart


# ── through the Actor (main._actor_wizard_upload / upload_only) ──────


class _FakeActor:
    """Apify Actor stand-in: a key-value store with a JSON round trip + a log."""

    def __init__(self, broken_read=False):
        self.store: dict[str, str] = {}
        self.log = logging.getLogger("fake-actor")
        self.broken_read = broken_read

    async def get_value(self, key):
        if self.broken_read:
            raise RuntimeError("key-value store unavailable")
        return json.loads(self.store[key]) if key in self.store else None

    async def set_value(self, key, value, **kw):
        self.store[key] = json.dumps(value) if not isinstance(value, bytes) else json.dumps("<bytes>")


class _Health:
    def __init__(self):
        self.events = []

    def record_event(self, *a, **k):
        self.events.append(a)


def _actor_upload(actor, infos, monkeypatch, tmp_path, run_id="RUN_A", **kw):
    import main
    monkeypatch.setenv("APIFY_ACTOR_RUN_ID", run_id)
    monkeypatch.setattr(main.config, "OUTPUT_DIR", tmp_path)
    args = {"batch_tag": TAG, "enrich": False, "skip_trace": False, "health": _Health(),
            "kvs": _FakeActor(), "notify_slack": False}
    args.update(kw)
    return asyncio.run(main._actor_wizard_upload(actor, infos, [], **args))


def test_actor_nov12_run_sends_all_three_and_saves_one_entry_per_file(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    actor = _FakeActor()
    infos = _nov12(tmp_path)
    res, fails = _actor_upload(actor, infos, monkeypatch, tmp_path)
    assert res["success"] and fails == [] and len(ds.landed) == 3
    saved = json.loads(actor.store["DATASIFT_UPLOAD_PROGRESS"])
    assert saved["version"] == up.UPLOAD_PROGRESS_VERSION
    assert saved["run_id"] == "RUN_A" and saved["batch_tag"] == TAG
    assert [e["file"] for e in saved["uploaded"]] == [Path(i["path"]).name for i in infos]


def test_actor_restart_midway_finishes_the_job(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    actor = _FakeActor()
    a1, a2 = _dirs(tmp_path)
    ds.die_before_call = 2                     # Bell Foreclosure landed, then the host died
    with pytest.raises(HostDied):
        _actor_upload(actor, _nov12(a1, "120007"), monkeypatch, tmp_path)
    ds.die_before_call = None
    infos = _nov12(a2, "122007")
    res, fails = _actor_upload(actor, infos, monkeypatch, tmp_path)
    assert res["success"] and fails == []
    _assert_all_landed(ds, infos)
    assert len(ds.landed) == 3
    assert _files(ds)[1:] == [Path(infos[1]["path"]).name, Path(infos[2]["path"]).name]
    assert res["message"] == "Uploaded 2/3 CSVs (1 already uploaded before a host restart)"


def test_actor_finished_run_restarted_sends_nothing(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    actor = _FakeActor()
    infos = _nov12(tmp_path)
    _actor_upload(actor, infos, monkeypatch, tmp_path)
    res, fails = _actor_upload(actor, infos, monkeypatch, tmp_path)
    assert res["success"] and fails == [] and len(ds.landed) == 3 and ds.logins == 1
    assert res["counts"]["resumed"] == 3


def test_actor_does_not_trust_the_record_the_nov12_run_left(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    actor = _FakeActor()
    infos = _nov12(tmp_path)
    actor.store["DATASIFT_UPLOAD_PROGRESS"] = json.dumps({
        "uploaded": {"Foreclosure": up._csv_sha256(infos[1]["path"]),
                     "Fire Damage": up._csv_sha256(infos[2]["path"])},
        "enrich_done": False, "skip_trace_done": False, "batch_tag": TAG})
    res, fails = _actor_upload(actor, infos, monkeypatch, tmp_path)
    assert res["success"] and fails == [] and len(ds.landed) == 3


def test_actor_ignores_progress_saved_by_another_run(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    actor = _FakeActor()
    infos = _nov12(tmp_path)
    _actor_upload(actor, infos, monkeypatch, tmp_path, run_id="RUN_A")
    res, _ = _actor_upload(actor, infos, monkeypatch, tmp_path, run_id="RUN_B")
    assert res["success"] and len(ds.landed) == 6 and res["counts"]["resumed"] == 0


def test_actor_unreadable_progress_store_uploads_everything(tmp_path, monkeypatch):
    ds = FakeDataSift().install(monkeypatch)
    infos = _nov12(tmp_path)
    res, fails = _actor_upload(_FakeActor(broken_read=True), infos, monkeypatch, tmp_path)
    assert res["success"] and fails == [] and len(ds.landed) == 3


def test_upload_only_mode_with_two_files_for_one_list(tmp_path, monkeypatch):
    """The recovery path (`upload_only`): a manifest naming two CSVs for the
    same list, same label — both must go."""
    import main
    ds = FakeDataSift().install(monkeypatch)
    monkeypatch.setenv("SIFT_DS_TRACE", "0")
    monkeypatch.setenv("APIFY_ACTOR_RUN_ID", "RUN_A")
    monkeypatch.setattr(main.config, "OUTPUT_DIR", tmp_path)
    monkeypatch.chdir(tmp_path)
    actor, kvs = _FakeActor(), _FakeActor()
    bell = HEADER + f"210 Margarita Dr,Killeen,{TAG}\n"
    travis = HEADER + f"3501 Neal St,Austin,{TAG}\n508 Kemp St,Austin,{TAG}\n"
    kvs.store["bell_fc.csv"] = json.dumps(bell)
    kvs.store["travis_fc.csv"] = json.dumps(travis)
    kvs.store["UPLOAD_MANIFEST"] = json.dumps({"batch_tag": TAG, "csvs": [
        {"key": "bell_fc.csv", "label": "Foreclosure", "list_name": "Foreclosure"},
        {"key": "travis_fc.csv", "label": "Foreclosure", "list_name": "Foreclosure"}]})
    ok = asyncio.run(main._actor_upload_only(actor, kvs, enrich=False, skip_trace=False,
                                             health=_Health(), notify_slack=False))
    assert ok is True
    assert [(ln, data.decode()) for ln, data in ds.landed] == [("Foreclosure", bell), ("Foreclosure", travis)]
    assert json.loads(actor.store["UPLOAD_RESULT"])["message"] == "Uploaded 2/2 CSVs"

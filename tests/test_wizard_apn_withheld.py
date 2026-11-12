"""No APN ever reaches DataSift.

2026-11-12: an uploaded row whose APN differs from the APN DataSift holds for
that address creates a SECOND record at the address (41 of 43 such rows across
batches 10-21 … 11-12; 0 of 41 rows with a blank or equal APN). Travis rows
carried the county geo ID while DataSift stores the TCAD property ID.

Three layers, all tested here:
  1. the formatter no longer writes an APN column (every CSV created today),
  2. for older files that still have one, the wizard is sent a copy with the
     APN values emptied,
  3. and the APN column is never dragged onto DataSift's APN field.
Offline — the wizard page is a stub.
"""

import asyncio
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import datasift_formatter as fmt  # noqa: E402
import datasift_uploader as up  # noqa: E402
import list_validator as lv  # noqa: E402
from datasift_formatter import DATASIFT_COLUMNS  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

# The header of every DataSift CSV written before 2026-11-12: today's columns
# plus APN (it sat right after "Personal Representative").
_i = DATASIFT_COLUMNS.index("Personal Representative") + 1
LEGACY_COLUMNS = DATASIFT_COLUMNS[:_i] + ["APN"] + DATASIFT_COLUMNS[_i:]

PARCEL_IDS = ("02437207140000", "01050204210000", "167978", "R342298")


def _write(tmp_path, rows, name="datasift_travis_foreclosure_2026-11-12_122007.csv", bom=False):
    """An OLD-format file (with the APN column)."""
    p = tmp_path / name
    with open(p, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEGACY_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def _rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


ROWS = [
    {"Property Street Address": "12409 Waterford Run Way", "Property City": "Manor", "Property State": "TX",
     "Property ZIP Code": "78653", "FULL NAME/COMPANY/TRUST": "Choctaw American Insurance Inc",
     "Owner First Name": "", "Owner Last Name": "",
     "Mailing Street Address": "10900 NE 4th St", "APN": "02437207140000", "Lists": "Foreclosure",
     "Tags": "Courthouse Data,FTM,Foreclosure,travis,upload_2026-11-12",
     "Notes": '=== AUCTION ===\nSale: Tue 1/5/2027, "in 54 days"\n\nEntity: Choctaw, Inc'},
    {"Property Street Address": "3501 Neal St", "Property City": "Austin", "Property State": "TX",
     "Property ZIP Code": "78702", "APN": "", "Lists": "Foreclosure", "Tags": "FTM", "Notes": "line 1\nline 2"},
    {"Property Street Address": "1624 Barton Springs Rd", "Property City": "Austin", "Property State": "TX",
     "Property ZIP Code": "78704", "Owner First Name": "Gordon", "Owner Last Name": "Neelley",
     "APN": "01050204210000", "Lists": "Fire Damage", "Tags": "FTM", "Notes": ""},
]


# ── layer 1: CSVs created today have no APN column ───────────────────


def _notice(**kw) -> NoticeData:
    n = NoticeData()
    n.address, n.city, n.state, n.zip = "12409 Waterford Run Way", "Manor", "TX", "78653"
    n.owner_name = "Choctaw American Insurance Inc"
    n.notice_type, n.county, n.date_added = "foreclosure", "Travis", "2026-11-12"
    n.parcel_id = "02437207140000"
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def test_header_has_no_apn_or_parcel_column():
    lowered = [c.lower() for c in DATASIFT_COLUMNS]
    assert not any(c in ("apn", "parcel id", "parcel", "parcel_id") for c in lowered)
    assert len(DATASIFT_COLUMNS) == len(set(DATASIFT_COLUMNS)) == len(LEGACY_COLUMNS) - 1


def test_built_row_carries_no_apn_even_when_the_notice_has_a_parcel():
    row = fmt._build_row(_notice())
    assert "APN" not in row and set(row) <= set(DATASIFT_COLUMNS)
    assert "02437207140000" not in " ".join(str(v) for v in row.values())


def test_every_csv_the_formatter_writes_is_free_of_the_parcel_id(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "OUTPUT_DIR", tmp_path)
    notices = [
        _notice(),
        _notice(address="1624 Barton Springs Rd", city="Austin", zip="78704", owner_name="Gordon Neelley",
                notice_type="fire_damage", parcel_id="01050204210000"),
        _notice(address="2110 Clairidge Ave", city="Killeen", zip="76549", county="Bell",
                owner_name="Darlene Harris", parcel_id="167978"),
        _notice(address="709 Jagged Rock", city="Georgetown", zip="78628", county="Williamson",
                owner_name="John Bilanych", notice_type="probate", parcel_id="R342298"),
    ]
    infos = fmt.write_datasift_by_notice_type(notices, split_by_county=True, batch_tag="upload_2026-11-12")
    assert len(infos) >= 3
    for info in infos:
        path = Path(info["path"])
        text = path.read_text(encoding="utf-8")
        header = next(csv.reader(text.splitlines()))
        assert header == DATASIFT_COLUMNS and "APN" not in header, path.name
        for pid in PARCEL_IDS:
            assert pid not in text, f"{pid} leaked into {path.name}"
        assert up._wizard_copy(path) == (path, 0)        # nothing left to withhold


def _assert_no_parcel(path, label):
    text = Path(path).read_text(encoding="utf-8-sig")
    header = [h.strip().lower() for h in next(csv.reader(text.splitlines()))]
    assert not any(h in ("apn", "parcel id", "parcel", "parcel_id", "parcel number") for h in header), (label, header)
    for pid in PARCEL_IDS:
        assert pid not in text, f"{pid} leaked into the {label} file"


def test_every_writer_in_the_upload_path_is_free_of_the_parcel_id(tmp_path, monkeypatch):
    """One sweep over EVERY function that writes a DataSift-format CSV: the
    per-type lead files, the Heirs file (deep prospecting / obituary output),
    the single-file and DMs/Heirs split writers, the Sold/Resolved cleanup
    file, the REVIEW file, and the skip-trace orchestrator's upload file."""
    import json
    import skip_orchestrator as so
    monkeypatch.setattr(fmt, "OUTPUT_DIR", tmp_path)
    heirs = json.dumps([{"name": "Kid Neelley", "relationship": "child",
                         "signing_authority": True, "status": "unverified"}])
    notices = [
        _notice(),
        # a deceased owner with a deep-prospecting / obituary heir map
        _notice(address="1624 Barton Springs Rd", city="Austin", zip="78704", owner_name="Gordon Neelley",
                notice_type="probate", parcel_id="01050204210000", owner_deceased="yes",
                date_of_death="2026-05-01", decision_maker_name="Kid Neelley", heir_map_json=heirs,
                obituary_url="https://example.com/obit"),
        _notice(address="2110 Clairidge Ave", city="Killeen", zip="76549", county="Bell",
                owner_name="Darlene Harris", notice_type="tax_delinquent", parcel_id="167978"),
        _notice(address="709 Jagged Rock", city="Georgetown", zip="78628", county="Williamson",
                owner_name="John Bilanych", notice_type="lien", parcel_id="R342298"),
    ]
    written = {}
    for split in (False, True):
        for info in fmt.write_datasift_by_notice_type(notices, split_by_county=split, batch_tag="upload_2026-11-12"):
            written[f"by-type {info['label']} split={split}"] = info["path"]
    assert any("Heirs" in k for k in written), "the Heirs (deep prospecting) file was not produced"
    written["single file"] = fmt.write_datasift_csv(notices, filename="single.csv")
    for info in fmt.write_datasift_split_csvs(notices):
        written[f"split {info['label']}"] = info["path"]
    sold = fmt.write_cleanup_csv([_notice(notice_type="tax_delinquent", record_status="sold")])
    assert sold, "cleanup file not produced"
    written["cleanup"] = sold["path"]
    held = _notice(address="8505 Greenflint Ln", city="Austin", zip="78759", notice_type="lien")
    held.review_reason = "debtor is not the owner of record"
    review = fmt.write_review_csv([held])
    assert review, "review file not produced"
    written["review"] = review["path"]
    assert len(written) >= 8
    for label, path in written.items():
        _assert_no_parcel(path, label)
    # the skip-trace orchestrator's upload file has its own header
    cols = [c.lower() for c in so._output_columns(30, 5)]
    assert not any(c in ("apn", "parcel id", "parcel") for c in cols)


def test_archive_converter_drops_the_parcel_column_without_calling_it_a_loss():
    import convert_datasift_headers as conv
    old = {"Property Street Address": "12409 Waterford Run Way", "APN": "02437207140000",
           "Parcel ID": "02437207140000", "Tags": "FTM"}
    new = conv.convert_row(old)
    assert "APN" not in new and "Parcel ID" not in new and set(new) == set(DATASIFT_COLUMNS)
    assert "02437207140000" not in " ".join(str(v) for v in new.values())
    assert not [l for l in conv._audit_row(old, new, "old.csv", 1) if "APN" in l or "Parcel ID" in l]


def _findings(path):
    report = lv.validate_files([path])
    files = getattr(report, "files", None) or getattr(report, "file_reports", None) or []
    assert files, "validator returned no file report"
    return [(f.severity, f.kind) for fr in files for f in fr.findings]


def test_validator_accepts_todays_header_and_only_warns_on_an_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "OUTPUT_DIR", tmp_path)
    new = fmt.write_datasift_by_notice_type([_notice()])[0]["path"]
    codes = _findings(new)
    assert not any(c in ("header_mismatch", "retired_column") for _s, c in codes), codes
    old = _write(tmp_path, ROWS, name="datasift_foreclosure_2026-11-11_old.csv")
    codes = _findings(old)
    assert (lv.WARN, "retired_column") in codes, codes
    assert not any(c == "header_mismatch" for _s, c in codes), codes


# ── layer 2: an older file's APN values are withheld from the wizard ─


def test_older_files_are_still_guarded():
    assert up.WIZARD_BLANKED_COLUMNS == ("APN",)
    assert all(up._never_mapped(n) for n in ("APN", "Apn", "apn", " APN "))
    assert not any(up._never_mapped(n) for n in ("Tags", "Lists", "Notes", "Property Street Address"))


def test_copy_empties_only_the_apn_values(tmp_path):
    src = _write(tmp_path, ROWS)
    before = src.read_bytes()
    dest, emptied = up._wizard_copy(src)
    assert emptied == 2
    assert dest != src and dest.name == src.name        # the wizard shows the same file name
    assert src.read_bytes() == before                    # the file on disk is untouched
    a, b = _rows(src), _rows(dest)
    assert len(a) == len(b) == 3
    assert list(b[0].keys()) == LEGACY_COLUMNS            # the old header is kept, APN column included
    for ra, rb in zip(a, b):
        assert rb["APN"] == ""
        assert {k: v for k, v in ra.items() if k != "APN"} == {k: v for k, v in rb.items() if k != "APN"}
    assert b[0]["Notes"] == ROWS[0]["Notes"]              # multi-line, quotes and commas survive


def test_file_without_apn_values_is_sent_as_it_is(tmp_path):
    src = _write(tmp_path, [dict(r, APN="") for r in ROWS])
    assert up._wizard_copy(src) == (src, 0)


def test_file_without_an_apn_column_is_sent_as_it_is(tmp_path):
    p = tmp_path / "phones.csv"
    p.write_text("Property Street Address,Phone 1 Number\n1 Main St,5125550100\n", encoding="utf-8")
    assert up._wizard_copy(p) == (p, 0)


def test_nothing_to_withhold_when_disabled(tmp_path):
    src = _write(tmp_path, ROWS)
    assert up._wizard_copy(src, None) == (src, 0)
    assert up._wizard_copy(src, ()) == (src, 0)


def test_bom_and_header_case_are_handled(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes("﻿Property Street Address,apn ,Notes\r\n1 Main St,123,n\r\n".encode("utf-8"))
    dest, emptied = up._wizard_copy(p)
    assert emptied == 1
    assert dest.read_text(encoding="utf-8").splitlines() == ["Property Street Address,apn ,Notes", "1 Main St,,n"]


def test_unreadable_or_broken_file_falls_back_to_the_original(tmp_path):
    missing = tmp_path / "missing.csv"
    assert up._wizard_copy(missing) == (missing, 0)
    latin = tmp_path / "latin1.csv"
    latin.write_bytes(b"Property Street Address,APN\n1 Pe\xf1a St,123\n")
    assert up._wizard_copy(latin) == (latin, 0)


def test_ragged_rows_keep_their_shape(tmp_path):
    p = tmp_path / "ragged.csv"
    p.write_text("A,APN,C\n1,111,x\n2\n3,333\n", encoding="utf-8")
    dest, emptied = up._wizard_copy(p)
    assert emptied == 2
    with open(dest, newline="", encoding="utf-8") as f:
        assert list(csv.reader(f)) == [["A", "APN", "C"], ["1", "", "x"], ["2"], ["3", ""]]


def test_two_copies_of_same_named_files_do_not_collide(tmp_path):
    a = _write(tmp_path, ROWS[:1])
    d2 = tmp_path / "other"
    d2.mkdir()
    b = _write(d2, ROWS[2:], name=a.name)
    da, _ = up._wizard_copy(a)
    db, _ = up._wizard_copy(b)
    assert da != db and _rows(da)[0]["Property Street Address"] != _rows(db)[0]["Property Street Address"]


# ── through upload_csv: what actually reaches the wizard's file input ──


class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def set_input_files(self, path):
        self.page.sent.append(Path(path))

    async def click(self, *a, **k): pass


class _Page:
    url = "https://app.reisift.io/records/properties"

    def __init__(self):
        self.sent = []

    def locator(self, sel):
        return _Loc(self, sel)

    async def goto(self, *a, **k): pass
    async def wait_for_timeout(self, *a, **k): pass


def _drive_wizard(monkeypatch, path, **kw):
    """Run the real upload_csv with the wizard reduced to two screens: the
    upload-file step, then Review (do_finish=False stops there)."""
    page = _Page()
    steps = iter(["upload_file", "review"])

    async def _step(p):
        return next(steps)

    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    monkeypatch.setattr(up, "_screenshot", _noop)
    monkeypatch.setattr(up, "_dismiss_popups", _noop)
    monkeypatch.setattr(up, "_click_next_step", _true)
    monkeypatch.setattr(up, "_classify_wizard_step", _step)
    kw.setdefault("list_name", "Foreclosure")
    kw.setdefault("existing_list", True)
    res = asyncio.run(up.upload_csv(page, path, do_finish=False, **kw))
    assert res["success"] and res["steps_seen"] == ["upload_file", "review"], res
    return page, res


def test_upload_csv_add_mode_sends_the_copy(tmp_path, monkeypatch):
    src = _write(tmp_path, ROWS)
    before = src.read_bytes()
    page, res = _drive_wizard(monkeypatch, src)
    assert len(page.sent) == 1 and page.sent[0] != src and page.sent[0].name == src.name
    assert all(r["APN"] == "" for r in _rows(page.sent[0]))
    assert [r["Property Street Address"] for r in _rows(page.sent[0])] == [r["Property Street Address"] for r in ROWS]
    assert res["apn_values_withheld"] == 2
    assert src.read_bytes() == before


def test_upload_csv_sends_todays_files_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "OUTPUT_DIR", tmp_path)
    new = Path(fmt.write_datasift_by_notice_type([_notice()])[0]["path"])
    page, res = _drive_wizard(monkeypatch, new)
    assert page.sent == [new] and res["apn_values_withheld"] == 0


def test_upload_csv_can_be_told_to_send_the_file_untouched(tmp_path, monkeypatch):
    src = _write(tmp_path, ROWS)
    page, res = _drive_wizard(monkeypatch, src, blank_columns=None)
    assert page.sent == [src] and res["apn_values_withheld"] == 0


def test_update_mode_passes_are_left_alone(tmp_path, monkeypatch):
    """Tagging / phone passes ("Update data") match existing records on purpose
    and are not this fix's business."""
    src = _write(tmp_path, ROWS)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(up, "_fill_wizard_setup", _noop)
    page, res = _drive_wizard(monkeypatch, src, mode="update",
                              update_option="Tagging existing properties")
    assert page.sent == [src] and res["apn_values_withheld"] == 0


# ── layer 3: the mapping step never drags APN onto DataSift's APN field ──


class _MapLoc:
    def __init__(self, sel):
        self.sel = sel

    @property
    def first(self):
        return self

    async def count(self):
        return 1


class _MapPage:
    def locator(self, sel):
        return _MapLoc(sel)


def _run_mapping(monkeypatch, left):
    """Run the real _map_wizard_columns against a fake column list: a drag
    'consumes' the dragged name from the left list, like the real wizard."""
    left = list(left)
    dragged = []

    async def _names(page):
        return list(left)

    async def _drag(page, source, target):
        name = re.search(r'text-is\("(.+?)"\)', source.sel).group(1)
        dragged.append(name)
        if name in left:
            left.remove(name)
        return True

    async def _pairs(page):
        return []

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(up, "_unmapped_column_names", _names)
    monkeypatch.setattr(up, "_drag_card", _drag)
    monkeypatch.setattr(up, "_mapping_pairs", _pairs)
    monkeypatch.setattr(up, "_dump_mapping", _noop)
    mapped, still = asyncio.run(up._map_wizard_columns(_MapPage()))
    return mapped, still, dragged


def test_apn_is_never_dragged_onto_a_datasift_field(monkeypatch):
    mapped, still, dragged = _run_mapping(monkeypatch, ["Tags", "Lists", "APN"])
    assert mapped == ["Tags", "Lists"] and still == ["APN"]
    assert "APN" not in dragged
    # an upload with APN left unmapped is not treated as broken
    assert [c for c in up.REQUIRED_MAPPED_COLUMNS if c in still] == []


def test_apn_in_any_capitalisation_is_never_dragged(monkeypatch):
    """A cleaned county file may spell the column "Apn" (DataSift's own export
    spelling)."""
    for spelling in ("Apn", "apn"):
        mapped, still, dragged = _run_mapping(monkeypatch, ["Tags", spelling, "Lists"])
        assert mapped == ["Tags", "Lists"] and still == [spelling] and spelling not in dragged


def test_mapping_is_unchanged_for_a_file_without_apn(monkeypatch):
    mapped, still, dragged = _run_mapping(monkeypatch, ["Tags", "Lists"])
    assert mapped == ["Tags", "Lists"] and still == [] and dragged == ["Tags", "Lists"]

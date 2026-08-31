"""Austin BSC -> DataSift formatter wiring + notice builder tests (offline).

Run: PYTHONPATH=src python tests/test_bsc_formatter.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datasift_formatter import _build_tags, _build_row, NOTICE_TYPE_TO_LIST, _NOTICE_TYPE_LABELS  # noqa: E402
from enrichment_pipeline import _detect_condo  # noqa: E402
from notice_parser import NoticeData  # noqa: E402
from scrapers import bsc_austin as b  # noqa: E402


def _item(**kw):
    base = dict(case_ids=["CL 2026-083909"], other_ids=[], section="new",
                address_raw="9204 Curlew Drive", owner_raw="Thomas A. Rodriguez & Casimiro Rodriguez",
                recommendation="Repair residential structure within 45 days.", previous_action="",
                rec_class="repair", prior_order_class="", trv_number="", repeat_offender=False,
                multifamily=False, block_text="")
    base.update(kw)
    return b.BscItem(**base)


def _notice(**kw):
    item = kw.pop("item", None) or _item()
    args = dict(address="9204 Curlew Drive", addr_note="", zip5="78748", parcel="0426231212",
                owner_name="Thomas A. Rodriguez", entity_type="", c_o_name="", co_owners=["Casimiro Rodriguez"],
                cad_rec={"fullname": "RODRIGUEZ TOMAS A &", "source": "current_mailing",
                         "situsaddress": "9204 CURLEW DR", "scity": "AUSTIN", "sstate": "TX", "szip": "787485108",
                         "quickrefid": "04262312120000"},
                tax_owner_name="RODRIGUEZ TOMAS A &", flags=[], effective_cls="repair",
                is_returning=False, escalated_from="")
    args.update(kw)
    return b._build_notice(item, "2026-08-26", 479428, **args)


def test_build_notice_core_fields():
    n = _notice()
    assert n.notice_type == "condemned" and n.county == "Travis"
    assert n.city == "Austin" and n.zip == "78748" and n.parcel_id == "0426231212"
    assert n.case_id == "CL 2026-083909" and n.order_class == "repair"
    assert n.severity == "structure_condition"
    assert n.date_added == "2026-08-26"
    assert n.compliance_deadline == "2026-10-10"          # 45 days after the hearing
    assert n.source_url.endswith("document.cfm?id=479428#case=CL-2026-083909")
    assert "CL 2026-083909" in n.violation_description and "08/26/2026" in n.violation_description
    assert "co-owner(s): Casimiro Rodriguez" in n.violation_description
    assert n.owner_street == "9204 Curlew Dr" and n.owner_zip == "78748"   # CAD mailing
    assert n.tax_owner_name == "RODRIGUEZ TOMAS A &"
    assert n.case_status.startswith("Open - new case")
    assert not n.assessed_value                          # current_mailing carries no value


def test_build_notice_caretaker_and_entity_and_delq():
    n = _notice(owner_name="Carlos Oviedo", c_o_name="Alex Vasquez", co_owners=[])
    assert n.decision_maker_name == "Alex Vasquez"
    assert n.decision_maker_source == "bsc_agenda_c_o" and n.dm_relationship == "care_of_caretaker"
    n = _notice(owner_name="Brugge 2507 LLC", entity_type="business", co_owners=[],
                cad_rec={"fullname": "BRUGGE 2507 LLC", "source": "delinquent_situs",
                         "totalpropmktvalue": "1234567", "delinquent_total": "5000", "years_delinquent": "2"})
    assert n.entity_type == "business"
    assert n.assessed_value == "1234567" and n.assessed_source == "travis_cad"
    assert n.tax_delinquent_amount == "5000" and not n.owner_street


def test_tags_and_row():
    n = _notice()
    tags = _build_tags(n).split(",")
    assert "Condemned" in tags and "bsc_repair" in tags and "code_severity_structure_condition" in tags
    assert "travis" in tags and "2026-08" in tags
    assert "bsc_returning" not in tags and "multifamily" not in tags
    row = _build_row(n)
    assert row["Lists"] == "Condemned"
    assert NOTICE_TYPE_TO_LIST["condemned"] == "Condemned" and _NOTICE_TYPE_LABELS["condemned"] == "Condemned"
    assert "CL 2026-083909" in row["Notes"] and "Violation:" in row["Notes"]
    assert row["Owner First Name"] == "Thomas" and row["Owner Last Name"] == "Rodriguez"

    esc = _notice(item=_item(section="returning", rec_class="demolish", repeat_offender=True,
                             recommendation="Issue new order for demolition of the structures.",
                             previous_action="Order TRV 2025142639 issued December 9, 2025, for repair"),
                  addr_note="Building 11", effective_cls="demolish", is_returning=True,
                  escalated_from="repair", flags=["multifamily"])
    tags = _build_tags(esc).split(",")
    for t in ("bsc_demolish", "bsc_returning", "bsc_escalated", "repeat_offender", "multifamily"):
        assert t in tags, t
    assert "TRV 2025142639" in esc.violation_description
    assert esc.case_status == "Open - returning, escalated repair->demolish"


def test_condo_filter_exempts_condemned():
    n = NoticeData(notice_type="condemned", address="1601 Royal Crest Dr",
                   raw_text="Address: 1601 Royal Crest Drive BLDG 3 UNIT 101")
    assert _detect_condo(n) is False
    cv = NoticeData(notice_type="code_violation", address="1601 Royal Crest Dr",
                    raw_text="Address: 1601 Royal Crest Drive BLDG 3 UNIT 101")
    assert _detect_condo(cv) is True


def test_government_owner_dropped_at_write():
    from datasift_formatter import _is_government_owner
    n = _notice(owner_name="City of Austin", entity_type="government", co_owners=[])
    assert _is_government_owner(n)


def test_church_routes_to_business_column_not_dropped():
    from datasift_formatter import _is_government_owner
    n = _notice(owner_name="St Johns Reg Baptist", entity_type="business", co_owners=[],
                tax_owner_name="ST JOHNS REG BAPTIST",
                cad_rec={"fullname": "ST JOHNS REG BAPTIST", "source": "current_mailing",
                         "situsaddress": "7501 BLESSING AVE", "scity": "AUSTIN", "sstate": "TX", "szip": "78752"})
    assert not _is_government_owner(n)
    row = _build_row(n)
    assert row["Owner First Name"] == "" and row["Owner Last Name"] == ""
    assert "Baptist" in row["FULL NAME/COMPANY/TRUST"]
    church = _notice(owner_name="Magnolia Christian Church", entity_type="business", co_owners=[],
                     tax_owner_name="", cad_rec=None)
    assert not _is_government_owner(church)
    row = _build_row(church)
    assert row["Owner Last Name"] == "" and row["FULL NAME/COMPANY/TRUST"] == "Magnolia Christian Church"


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
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

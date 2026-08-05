"""Tests for obituary_enricher.py — name parsing and decision-maker logic."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from obituary_enricher import (
    parse_tax_owner_name, identify_decision_maker, _is_obituary_url,
    rank_decision_makers, _extract_structured_text, _is_listing_url,
    _extract_personal_from_trust_estate, _get_name_variants,
    _parse_notice_owner_name, _middle_name_conflict, _validate_obituary_match,
    _norm_state, _dod_sanity_check, _estate_display_name,
)
from notice_parser import NoticeData


# ── Name parsing tests ──────────────────────────────────────────────


def test_simple_name():
    assert parse_tax_owner_name("WILLIAMS DANIEL H") == ["Daniel H Williams"]


def test_name_no_middle():
    assert parse_tax_owner_name("SMITH JOHN") == ["John Smith"]


def test_life_estate_suffix():
    """Suffix stripped, no middle initial → just first last."""
    assert parse_tax_owner_name("JONES ROBERT (LIFE EST)") == ["Robert Jones"]


def test_life_estate_no_parens():
    assert parse_tax_owner_name("HALL BRENDA J LIFE ESTATE") == ["Brenda J Hall"]


def test_personal_rep_suffix():
    assert parse_tax_owner_name("DOE JANE PERSONAL REPRESENTATIVE") == ["Jane Doe"]


def test_trustee_suffix():
    assert parse_tax_owner_name("SMITH JOHN TRUSTEE") == ["John Smith"]


def test_et_al_suffix():
    assert parse_tax_owner_name("MCGHEE JAMES M ET AL") == ["James M Mcghee"]


def test_care_of():
    """Care-of (%) — only search the primary name (before %)."""
    result = parse_tax_owner_name("BLALOCK GARY W % BLALOCK MISTY D")
    assert result == ["Gary W Blalock"]


def test_joint_owners_same_last():
    """Joint owners with & and same last name."""
    result = parse_tax_owner_name("WILLIAMS DANIEL H & CHRISTINE C")
    assert len(result) == 2
    assert result[0] == "Daniel H Williams"
    assert result[1] == "Christine Williams"


def test_joint_owners_different_last():
    """Joint owners with & and different last names."""
    result = parse_tax_owner_name("SMITH JOHN A & JONES JANE B")
    assert len(result) == 2
    assert result[0] == "John A Smith"
    assert result[1] == "Jane B Jones"


def test_business_entity_skipped():
    """Business entities should return empty list."""
    assert parse_tax_owner_name("EASTSIDE REAL ESTATE AND DEVELOPMENT GROUP LLC") == []
    assert parse_tax_owner_name("FIRST TENNESSEE BANK") == []
    assert parse_tax_owner_name("ABC HOLDINGS LLC") == []


def test_empty_input():
    assert parse_tax_owner_name("") == []
    assert parse_tax_owner_name("   ") == []
    assert parse_tax_owner_name(None) == []


def test_single_word():
    """Single word names should return empty (can't split first/last)."""
    assert parse_tax_owner_name("SMITH") == []


# ── Decision-maker identification tests ──────────────────────────────


def test_dm_executor_first():
    """Executor takes priority over all others."""
    survivors = [
        {"name": "Jane Smith", "relationship": "wife"},
        {"name": "Bob Smith", "relationship": "son and executor"},
    ]
    name, rel = identify_decision_maker(survivors)
    assert name == "Bob Smith"
    assert "executor" in rel


def test_dm_spouse_over_children():
    survivors = [
        {"name": "Mitchell Williams", "relationship": "son"},
        {"name": "Jane Williams", "relationship": "wife"},
    ]
    name, rel = identify_decision_maker(survivors)
    assert name == "Jane Williams"
    assert rel == "wife"


def test_dm_oldest_child():
    """First listed child when no spouse."""
    survivors = [
        {"name": "Mitchell Williams", "relationship": "son"},
        {"name": "Danielle Sykes", "relationship": "daughter"},
    ]
    name, rel = identify_decision_maker(survivors)
    assert name == "Mitchell Williams"
    assert rel == "son"


def test_dm_sibling_fallback():
    """Sibling when no spouse or children."""
    survivors = [
        {"name": "Margaret Nevels", "relationship": "sister"},
        {"name": "DruAnna Overbay", "relationship": "sister"},
    ]
    name, rel = identify_decision_maker(survivors)
    assert name == "Margaret Nevels"
    assert rel == "sister"


def test_dm_empty():
    assert identify_decision_maker([]) == ("", "")


def test_dm_stokely_ln_example():
    """Real data from 5100 Stokely Ln research pack."""
    survivors = [
        {"name": "Mitchell Keith Williams", "relationship": "son"},
        {"name": "Danielle Williams Sykes", "relationship": "daughter"},
        {"name": "Meghan Michelle Williams", "relationship": "daughter"},
    ]
    name, rel = identify_decision_maker(survivors)
    assert name == "Mitchell Keith Williams"
    assert rel == "son"


# ── URL detection tests ──────────────────────────────────────────────


def test_obituary_url_legacy():
    assert _is_obituary_url("https://www.legacy.com/us/obituaries/john-smith") is True


def test_obituary_url_dignity():
    assert _is_obituary_url("https://www.dignitymemorial.com/obituaries/knoxville-tn/daniel-williams-12461233") is True


def test_obituary_url_generic():
    assert _is_obituary_url("https://www.somefuneralhome.com/obituaries/john-smith") is True


def test_non_obituary_url():
    assert _is_obituary_url("https://www.google.com/search?q=test") is False
    assert _is_obituary_url("https://www.facebook.com/john.smith") is False


# ── rank_decision_makers tests ────────────────────────────────────────


def test_rank_executor_first():
    """Executor ranks above spouse and children."""
    survivors = [
        {"name": "Jane Smith", "relationship": "wife"},
        {"name": "Tom Smith", "relationship": "son"},
    ]
    ranked = rank_decision_makers(survivors, executor_name="Bob Smith")
    assert ranked[0]["name"] == "Bob Smith"
    assert ranked[0]["relationship"] == "executor"
    assert ranked[1]["name"] == "Jane Smith"


def test_rank_verified_living_before_unverified():
    """Within same priority group, verified_living sorts before unverified."""
    survivors = [
        {"name": "Alice Jones", "relationship": "daughter"},
        {"name": "Bob Jones", "relationship": "son"},
    ]
    statuses = {"Alice Jones": "unverified", "Bob Jones": "verified_living"}
    ranked = rank_decision_makers(survivors, heir_statuses=statuses)
    assert ranked[0]["name"] == "Bob Jones"
    assert ranked[0]["status"] == "verified_living"
    assert ranked[1]["name"] == "Alice Jones"
    assert ranked[1]["status"] == "unverified"


def test_rank_deceased_last():
    """Deceased heirs rank after living and unverified within same group."""
    survivors = [
        {"name": "A", "relationship": "son"},
        {"name": "B", "relationship": "son"},
        {"name": "C", "relationship": "son"},
    ]
    statuses = {"A": "deceased", "B": "verified_living", "C": "unverified"}
    ranked = rank_decision_makers(survivors, heir_statuses=statuses)
    assert ranked[0]["name"] == "B"  # living first
    assert ranked[1]["name"] == "C"  # unverified second
    assert ranked[2]["name"] == "A"  # deceased last


def test_rank_spouse_over_children():
    """Spouse priority group ranks above children."""
    survivors = [
        {"name": "Tom", "relationship": "son"},
        {"name": "Jane", "relationship": "wife"},
    ]
    ranked = rank_decision_makers(survivors)
    assert ranked[0]["name"] == "Jane"
    assert ranked[1]["name"] == "Tom"


def test_rank_empty():
    assert rank_decision_makers([]) == []
    assert rank_decision_makers([], executor_name="") == []


def test_rank_numbering():
    """Each entry gets a 1-based rank number."""
    survivors = [
        {"name": "A", "relationship": "wife"},
        {"name": "B", "relationship": "son"},
        {"name": "C", "relationship": "daughter"},
    ]
    ranked = rank_decision_makers(survivors)
    assert ranked[0]["rank"] == 1
    assert ranked[1]["rank"] == 2
    assert ranked[2]["rank"] == 3


def test_rank_dedup_executor_in_survivors():
    """Executor name also appearing in survivors list should not duplicate."""
    survivors = [
        {"name": "Bob Smith", "relationship": "son and executor"},
        {"name": "Jane Smith", "relationship": "wife"},
    ]
    ranked = rank_decision_makers(survivors, executor_name="Bob Smith")
    names = [r["name"] for r in ranked]
    assert names.count("Bob Smith") == 1


def test_rank_source_field():
    """All ranked DMs from survivors get source='obituary_survivors'."""
    survivors = [{"name": "Jane", "relationship": "wife"}]
    ranked = rank_decision_makers(survivors)
    assert ranked[0]["source"] == "obituary_survivors"


# ── _apply_obituary_match tests ──────────────────────────────────────


def test_apply_match_ranked_dms():
    """Ranked DMs populate DM 1/2/3 fields on NoticeData."""
    from obituary_enricher import _apply_obituary_match

    notice = NoticeData()
    parsed = {"full_name": "John Doe", "date_of_death": "2025-01-15"}
    ranked = [
        {"name": "Jane Doe", "relationship": "wife", "status": "verified_living", "source": "obituary_survivors"},
        {"name": "Tom Doe", "relationship": "son", "status": "unverified", "source": "obituary_survivors"},
        {"name": "Sue Doe", "relationship": "daughter", "status": "unverified", "source": "obituary_survivors"},
    ]
    error = {
        "heir_search_depth": 1,
        "heirs_verified_living": 1,
        "heirs_verified_deceased": 0,
        "heirs_unverified": 2,
        "dm_confidence": "high",
        "dm_confidence_reason": "1 verified living heir(s)",
        "missing_flags": [],
    }
    _apply_obituary_match(notice, parsed, "http://example.com/obit", "full_page", ranked, error)

    assert notice.owner_deceased == "yes"
    assert notice.decision_maker_name == "Jane Doe"
    assert notice.decision_maker_status == "verified_living"
    assert notice.decision_maker_2_name == "Tom Doe"
    assert notice.decision_maker_2_status == "unverified"
    assert notice.decision_maker_3_name == "Sue Doe"
    assert notice.dm_confidence == "high"
    assert notice.heirs_verified_living == "1"
    assert notice.heirs_unverified == "2"
    assert notice.obituary_source_type == "full_page"


def test_apply_match_no_ranked_dms_fallback():
    """Without ranked DMs, falls back to simple single-DM pick."""
    from obituary_enricher import _apply_obituary_match

    notice = NoticeData()
    parsed = {
        "full_name": "John Doe",
        "date_of_death": "2025-06-01",
        "survivors": [{"name": "Jane Doe", "relationship": "wife"}],
        "executor_named": "",
    }
    _apply_obituary_match(notice, parsed, "http://example.com/obit", "snippet")

    assert notice.owner_deceased == "yes"
    assert notice.decision_maker_name == "Jane Doe"
    assert notice.decision_maker_status == "unverified"
    assert notice.obituary_source_type == "snippet"
    assert notice.heir_search_depth == "0"
    assert notice.dm_confidence == "medium"


def test_apply_match_snippet_no_survivors():
    """Snippet match with no survivors sets low confidence + flags."""
    from obituary_enricher import _apply_obituary_match

    notice = NoticeData()
    parsed = {
        "full_name": "John Doe",
        "date_of_death": "",
        "survivors": [],
        "executor_named": "",
    }
    _apply_obituary_match(notice, parsed, "http://example.com/obit", "snippet")

    assert notice.owner_deceased == "yes"
    assert notice.decision_maker_name == ""
    assert notice.dm_confidence == "low"
    assert "snippet_only" in notice.missing_data_flags
    assert "no_survivors" in notice.missing_data_flags


# ── _extract_structured_text tests ──────────────────────────────────


def test_extract_jsonld_article_body():
    """JSON-LD with @type NewsArticle extracts articleBody."""
    html = '''<html><head>
    <script type="application/ld+json">
    {"@type": "NewsArticle", "articleBody": "John Smith, age 78, of Knoxville, passed away on January 15, 2025. He is survived by his wife Jane Smith; son Tom Smith; daughter Sue Smith."}
    </script>
    </head><body><div id="app"></div></body></html>'''
    text = _extract_structured_text(html, "https://www.legacy.com/us/obituaries/john-smith")
    assert "John Smith" in text
    assert "survived by" in text
    assert "Jane Smith" in text
    assert len(text) > 100


def test_extract_jsonld_html_tags_cleaned():
    """HTML tags in articleBody are stripped."""
    html = '''<html><head>
    <script type="application/ld+json">
    {"@type": "NewsArticle", "articleBody": "John Smith<br/>Age 78<br/><b>Knoxville</b>, TN. Survived by wife <i>Jane</i>. He loved his family and friends dearly and will be missed by all who knew him very much."}
    </script>
    </head><body></body></html>'''
    text = _extract_structured_text(html, "https://legacy.com/obit")
    assert "<br" not in text
    assert "<b>" not in text
    assert "<i>" not in text
    assert "John Smith" in text


def test_extract_jsonld_array_format():
    """JSON-LD as array of objects."""
    html = '''<html><head>
    <script type="application/ld+json">
    [{"@type": "WebPage"}, {"@type": "NewsArticle", "articleBody": "Mary Johnson, 82, of Maryville, Tennessee, passed away February 10, 2025. She is survived by her son Robert Johnson and daughter Patricia Miller."}]
    </script>
    </head><body></body></html>'''
    text = _extract_structured_text(html, "https://legacy.com/obit")
    assert "Mary Johnson" in text
    assert "survived by" in text


def test_extract_initial_state():
    """window.__INITIAL_STATE__ extraction for legacy.com old format."""
    html = '''<html><head><script>
    window.__INITIAL_STATE__ = {"personStore": {"displayText": {"text": "Robert Williams, 65, of Knoxville, Tennessee, went to be with the Lord on March 5, 2025. He is survived by his loving wife Christine Williams; children Daniel and Sarah."}}};</script>
    </head><body></body></html>'''
    text = _extract_structured_text(html, "https://legacy.com/obit")
    assert "Robert Williams" in text
    assert "Christine Williams" in text


def test_extract_short_text_rejected():
    """Structured text shorter than 100 chars is rejected."""
    html = '''<html><head>
    <script type="application/ld+json">
    {"@type": "NewsArticle", "articleBody": "Short text."}
    </script>
    </head><body></body></html>'''
    text = _extract_structured_text(html, "https://legacy.com/obit")
    assert text == ""


def test_extract_no_structured_data():
    """Plain HTML without structured data returns empty string."""
    html = '<html><body><p>Hello world</p></body></html>'
    text = _extract_structured_text(html, "https://example.com")
    assert text == ""


def test_extract_invalid_json():
    """Malformed JSON in ld+json doesn't crash."""
    html = '''<html><head>
    <script type="application/ld+json">{not valid json}</script>
    </head><body></body></html>'''
    text = _extract_structured_text(html, "https://legacy.com/obit")
    assert text == ""


# ── _is_listing_url tests ──────────────────────────────────────────


def test_listing_url_legacy_local():
    assert _is_listing_url("https://www.legacy.com/us/obituaries/local/tennessee/knoxville-area") is True


def test_listing_url_search():
    assert _is_listing_url("https://www.legacy.com/search?q=john+smith") is True


def test_listing_url_specific_obit():
    assert _is_listing_url("https://www.legacy.com/us/obituaries/name/john-smith/12345") is False


def test_listing_url_dignity():
    assert _is_listing_url("https://www.dignitymemorial.com/obituaries/knoxville-tn/john-smith-12345") is False


# ── no_dm_possible error info applied correctly ────────────────────


def test_apply_match_no_dm_possible():
    """Tier 5: no_dm_possible error_info sets correct flags on NoticeData."""
    from obituary_enricher import _apply_obituary_match

    notice = NoticeData()
    parsed = {
        "full_name": "John Doe",
        "date_of_death": "2025-03-01",
        "survivors": [],
        "executor_named": "",
    }
    error = {
        "heir_search_depth": 0,
        "heirs_verified_living": 0,
        "heirs_verified_deceased": 0,
        "heirs_unverified": 0,
        "missing_flags": ["no_survivors", "no_dm_possible"],
        "dm_confidence": "none",
        "dm_confidence_reason": "obituary confirmed but no family members identifiable",
    }
    _apply_obituary_match(notice, parsed, "http://example.com/obit", "snippet", None, error)

    assert notice.owner_deceased == "yes"
    assert notice.decision_maker_name == ""
    assert notice.dm_confidence == "none"
    assert "no_dm_possible" in notice.missing_data_flags
    assert "no_survivors" in notice.missing_data_flags
    assert notice.dm_confidence_reason == "obituary confirmed but no family members identifiable"


# ── Trust/estate name extraction tests ──────────────────────────────


def test_trust_simple():
    assert _extract_personal_from_trust_estate("JOHN DOE TRUST") == "JOHN DOE"


def test_trust_revocable():
    assert _extract_personal_from_trust_estate("THE JOHN DOE REVOCABLE TRUST") == "JOHN DOE"


def test_trust_living():
    assert _extract_personal_from_trust_estate("MARY SMITH LIVING TRUST") == "MARY SMITH"


def test_trust_revocable_living():
    assert _extract_personal_from_trust_estate("ROBERT JONES REVOCABLE LIVING TRUST") == "ROBERT JONES"


def test_estate_of():
    assert _extract_personal_from_trust_estate("ESTATE OF MARY SMITH") == "MARY SMITH"


def test_estate_of_the():
    assert _extract_personal_from_trust_estate("THE ESTATE OF JOHN A DOE") == "JOHN A DOE"


def test_trust_business_entity():
    """Business entity trusts should return None."""
    assert _extract_personal_from_trust_estate("FIRST TENNESSEE BANK TRUST") is None


def test_trust_single_word():
    """Single word before TRUST should return None (not enough for first+last)."""
    assert _extract_personal_from_trust_estate("SMITH TRUST") is None


def test_trust_in_tax_name_parsing():
    """Trust names should extract personal name in parse_tax_owner_name."""
    result = parse_tax_owner_name("DOE JOHN TRUST")
    assert result == ["John Doe"]


def test_estate_in_tax_name_parsing():
    """Estate names should extract personal name in parse_tax_owner_name."""
    result = parse_tax_owner_name("ESTATE OF MARY SMITH")
    assert result == ["Mary Smith"]


def test_trust_in_notice_name_parsing():
    """Notice-format trust names should extract personal name."""
    result = _parse_notice_owner_name("John Doe Trust")
    assert result == ["John Doe"]


def test_estate_in_notice_name_parsing():
    result = _parse_notice_owner_name("Estate Of Mary Smith")
    assert result == ["Mary Smith"]


# ── Nickname variant tests ──────────────────────────────────────────


def test_nickname_robert_to_bob():
    variants = _get_name_variants("Robert")
    assert "bob" in variants
    assert "rob" in variants


def test_nickname_bob_to_robert():
    variants = _get_name_variants("Bob")
    assert "robert" in variants


def test_nickname_unknown_name():
    """Unknown names should return empty list."""
    assert _get_name_variants("Zyxwvut") == []


def test_nickname_william_to_bill():
    variants = _get_name_variants("William")
    assert "bill" in variants
    assert "will" in variants


def test_nickname_case_insensitive():
    """Should work regardless of case."""
    variants = _get_name_variants("MARGARET")
    assert "maggie" in variants or "peggy" in variants


# ── Match-validation gate: middle-name veto (fix #2) ────────────────


def test_middle_name_conflict_initial_vs_full():
    # 'Michael T Conner' vs 'Michael Ralph Conner' — different person.
    assert _middle_name_conflict("Michael T Conner", "Michael Ralph Conner") is True


def test_middle_name_conflict_tax_format():
    # Surname-first tax name compares correctly (word order irrelevant).
    assert _middle_name_conflict("CONNER MICHAEL T", "Michael Ralph Conner") is True


def test_middle_name_owner_missing_middle_is_tolerant():
    assert _middle_name_conflict("Michael Conner", "Michael Ralph Conner") is False


def test_middle_name_obit_missing_middle_is_tolerant():
    assert _middle_name_conflict("Robert Lee Jones", "Robert Jones") is False


def test_middle_name_initial_compatible():
    # 'B' is compatible with 'Bruce'.
    assert _middle_name_conflict("John B Thornton", "John Bruce Thornton") is False


def test_middle_name_identical():
    assert _middle_name_conflict("John Bruce Thornton", "John Bruce Thornton") is False


def test_middle_name_two_full_middles_conflict():
    assert _middle_name_conflict("Mary Jane Smith", "Mary Ann Smith") is True


def test_validate_rejects_middle_conflict():
    n = NoticeData(owner_name="Michael T Conner", city="Waller", state="TX",
                   date_added="2025-07-03")
    parsed = {"full_name": "Michael Ralph Conner", "confidence": "high",
              "date_of_death": "2025-07-03", "state": "TX"}
    ok, why = _validate_obituary_match(parsed, n, "Michael T Conner", "snippet")
    assert ok is False and "middle-name" in why


# ── Match-validation gate: geographic veto (fix #3) ─────────────────


def test_norm_state():
    assert _norm_state("Kansas") == "KS"
    assert _norm_state("tx") == "TX"
    assert _norm_state("TX") == "TX"
    assert _norm_state("zzz") == ""
    assert _norm_state("") == ""


def test_validate_rejects_out_of_state_no_tie():
    n = NoticeData(owner_name="John Bruce Thornton", city="Austin", state="TX",
                   date_added="2025-06-01")
    parsed = {"full_name": "John Bruce Thornton", "confidence": "high",
              "state": "KS", "city": "Wichita"}
    ok, why = _validate_obituary_match(parsed, n, "John Bruce Thornton", "full_page", "")
    assert ok is False and "geographic" in why


def test_validate_allows_out_of_state_with_texas_tie():
    n = NoticeData(owner_name="John Bruce Thornton", city="Austin", state="TX",
                   date_added="2025-06-01")
    parsed = {"full_name": "John Bruce Thornton", "confidence": "high",
              "state": "KS", "city": "Wichita"}
    text = "John moved to Wichita after decades in Austin, Texas."
    ok, _ = _validate_obituary_match(parsed, n, "John Bruce Thornton", "full_page", text)
    assert ok is True


def test_validate_allows_texas_obituary():
    n = NoticeData(owner_name="John Bruce Thornton", city="Austin", state="TX",
                   date_added="2025-06-01")
    parsed = {"full_name": "John Bruce Thornton", "confidence": "high",
              "state": "TX", "city": "Austin"}
    ok, _ = _validate_obituary_match(parsed, n, "John Bruce Thornton", "full_page", "")
    assert ok is True


def test_validate_no_state_no_veto():
    n = NoticeData(owner_name="John Bruce Thornton", city="Austin", state="TX",
                   date_added="2025-06-01")
    parsed = {"full_name": "John Bruce Thornton", "confidence": "high",
              "state": "", "city": ""}
    ok, _ = _validate_obituary_match(parsed, n, "John Bruce Thornton", "full_page", "")
    assert ok is True


# ── DOD sanity guard (fix #2) ───────────────────────────────────────


def test_dod_after_record_date_rejected():
    # DOD materially after the record date is impossible / a parse artifact.
    n = NoticeData(owner_name="X", date_added="2025-06-01")
    assert _dod_sanity_check("2025-07-15", n) is False


def test_dod_same_day_allowed():
    # Same-day (within the 7-day skew window) is not rejected by this guard alone.
    n = NoticeData(owner_name="X", date_added="2025-07-03")
    assert _dod_sanity_check("2025-07-03", n) is True


def test_dod_recent_past_allowed():
    n = NoticeData(owner_name="X", date_added="2025-06-01")
    assert _dod_sanity_check("2025-03-29", n) is True


def test_dod_too_old_rejected():
    n = NoticeData(owner_name="X", date_added="2025-06-01")
    assert _dod_sanity_check("2018-01-01", n) is False


# ── _estate_display_name tests (Estate-of fallback name resolution) ──


def test_estate_name_from_tax_owner_when_owner_blank():
    """code_violation regression: owner_name blank, name lives in tax_owner_name
    (NAMELF). Must resolve to First-Last, not leave 'Estate of ' nameless.
    Repro of 912 Romeria Dr (parcel 0229071232)."""
    n = NoticeData(notice_type="code_violation", county="Travis",
                   address="912 Romeria Dr", owner_name="",
                   tax_owner_name="ROSE TABITHA JILL BLEVINS")
    assert _estate_display_name(n) == "Tabitha Rose"


def test_estate_name_prefers_owner_name():
    """owner_name (already First-Last) wins — no regression for probate/foreclosure."""
    n = NoticeData(owner_name="Jane Doe", tax_owner_name="SMITH JOHN")
    assert _estate_display_name(n) == "Jane Doe"


def test_estate_name_decedent_before_tax_owner():
    """decedent_name used when owner_name blank, before tax_owner_name."""
    n = NoticeData(owner_name="", decedent_name="Mary Alice Rowe",
                   tax_owner_name="SMITH JOHN")
    assert _estate_display_name(n) == "Mary Alice Rowe"


def test_estate_name_empty_when_nothing_usable():
    """No usable name anywhere → '' so caller keeps prior 'Estate of ' (no fabrication)."""
    n = NoticeData(owner_name="", decedent_name="", tax_owner_name="")
    assert _estate_display_name(n) == ""


def test_estate_name_business_tax_owner_returns_empty():
    """Unparseable/business NAMELF must NOT be routed in as a person name."""
    n = NoticeData(owner_name="", tax_owner_name="EASTSIDE REAL ESTATE LLC")
    assert _estate_display_name(n) == ""


if __name__ == "__main__":
    passed = 0
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                passed += 1
                print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

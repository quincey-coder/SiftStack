"""Format NoticeData records into DataSift.ai (REISift) upload-ready CSV.

DataSift has 60+ built-in fields that auto-map when CSV headers match exactly.
This module maps our enrichment data to those built-in fields, plus 23 custom
fields in the "SiftStack" custom group for deep prospecting/notice-specific data.

For deceased records, the contact (Owner First/Last + Mailing Address) is set
to the decision maker, not the deceased owner. For living records, the contact
is the property owner.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# Column order: auto-mapped built-in fields first, then custom fields.
# Headers must match DataSift's exact names for auto-mapping during upload.
DATASIFT_COLUMNS = [
    # ── Core (auto-mapped) ──
    "Property Street Address",
    "Property City",
    "Property State",
    "Property ZIP Code",
    "Business Name",
    "Owner First Name",
    "Owner Last Name",
    "Mailing Street Address",
    "Mailing City",
    "Mailing State",
    "Mailing ZIP Code",
    # ── Phone/Email (Tracerfy skip trace, mapped to DataSift built-in) ──
    "Phone 1",
    "Phone 2",
    "Phone 3",
    "Phone 4",
    "Phone 5",
    "Phone 6",
    "Phone 7",
    "Phone 8",
    "Phone 9",
    "Email 1",
    "Email 2",
    "Email 3",
    "Email 4",
    "Email 5",
    "Tags",
    "Lists",
    "Notes",
    # ── Built-in fields (auto-mapped by DataSift) ──
    "Estimated Value",
    "MSL Status",               # DataSift spells it "MSL" not "MLS"
    "Last Sale Date",
    "Last Sale Price",
    "Equity Percentage",
    "Tax Deliquent Value",      # DataSift typo — "Deliquent" not "Delinquent"
    "Tax Delinquent Year",
    "Tax Auction Date",
    "Foreclosure Date",
    "Probate Open Date",
    "Personal Representative",
    "Parcel ID",
    "Structure Type",
    "Year Built",
    "Living SqFt",
    "Bedrooms",
    "Bathrooms",
    "Lot (Acres)",
    # ── Custom fields ("Deceased & Heir Intelligence" group) ──
    # Headers MUST match the DataSift custom-field labels 1:1
    # (deceased_heir_fields.json) — upload auto-maps on exact label match.
    "Notice Type",
    "County",
    "Date Added",
    "Owner Deceased",
    "Date of Death",
    "Decedent Name",
    "Decision Maker (Name)",
    "DM Relationship",
    "Decision-Maker Confidence",
    "DM 2 Name / Relationship",
    "DM 3 Name / Relationship",
    "Obituary URL",
    "Source URL",
    # ── Deep prospecting fields ──
    "Decision-Maker Status",
    "DM Source",
    "DM 2 Status",
    "DM 3 Status",
    "Heir Count",
    "Heirs Living",
    "Signatures to Close",
    "Signing Chain Names",
    "DM Confidence Reason",
    "Data Flags",
    "Title Flag",
    # ── Entity research fields ──
    "Entity Type",
    "Entity Contact + Role",
]


def _format_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to M/D/YYYY."""
    if not iso_date:
        return ""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return iso_date


# ── Select-option normalization ─────────────────────────────────────
# DataSift select fields match option labels CASE-SENSITIVELY on import
# ("yes" drops silently where "Yes" stores — live-verified), so every value
# destined for a select column must ship as its exact option label.

_NOTICE_TYPE_LABELS = {
    "foreclosure": "Foreclosure",
    "tax_sale": "Tax Sale",
    "tax_delinquent": "Tax Delinquent",
    "probate": "Probate",
    "eviction": "Eviction",
    "code_violation": "Code Violation",
    "divorce": "Divorce",
    "lien": "Lien",
    "lis_pendens": "Lis Pendens",
    "fire_damage": "Fire Damage",
}

_OWNER_DECEASED_LABELS = {"yes": "Yes", "no": "No", "suspected": "Suspected"}

_DM_STATUS_LABELS = {
    "verified_living": "Verified Living",
    "unverified": "Unverified",
    "deceased": "Deceased",
    "unknown": "Unknown",
}

_CONFIDENCE_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}

_COUNTY_OPTIONS = {"Travis", "Bell", "Williamson"}


def _select_label(value: str, mapping: dict) -> str:
    """Normalize a raw enum to its select option label; blank stays blank.

    Unknown values pass through unchanged — visible in the CSV even though
    the select won't store them (closed sets from our own enrichment code).
    """
    v = (value or "").strip()
    if not v:
        return ""
    return mapping.get(v.lower(), v)


def _county_label(county: str) -> str:
    """Map county to its select option; unexpected non-blank → Other."""
    c = (county or "").strip().title()
    if not c:
        return ""
    return c if c in _COUNTY_OPTIONS else "Other"


def _title_flag(flags: str) -> str:
    """Derive the Title Flag select from the raw research/CAD flags.

    Life Estate outranks Et Al when both are present (it dictates the
    signer). No title complication → blank, so clean records stay
    unfiltered; the None/Other options exist for manual use.
    """
    f = (flags or "").lower()
    if "cad_life_estate" in f:
        return "Life Estate"
    if "cad_et_al" in f:
        return "Et Al"
    return ""


def _name_rel(name: str, rel: str) -> str:
    """Combine name + relationship/role into one readable value: "Name (rel)"."""
    name = (name or "").strip()
    rel = (rel or "").strip()
    if not name:
        return ""
    return f"{name} ({rel})" if rel else name


def _heir_count(notice: NoticeData) -> str:
    """Return total heir count from heir_map_json, or empty string."""
    if not notice.heir_map_json:
        return ""
    try:
        return str(len(json.loads(notice.heir_map_json)))
    except (json.JSONDecodeError, TypeError):
        return ""


# Entity suffixes that indicate a business, not a person.
# DataSift marks records incomplete if owner name contains these without a real person.
_ENTITY_SUFFIXES = re.compile(
    r"\b(?:LLC|L\.L\.C|Corp|Corporation|Inc|Incorporated|Trust|LP|LLP|"
    r"LTD|Limited|Co\b|Company|Association|Partners|Partnership|Holdings)\b",
    re.IGNORECASE,
)


def _is_entity_name(name: str) -> bool:
    """Return True if name looks like a business entity, not a person."""
    return bool(_ENTITY_SUFFIXES.search(name))


def _zip5(z: str) -> str:
    """Normalize a ZIP to 5 digits (strip ZIP+4 / dashes). Tax-roll mailing
    ZIPs arrive as ZIP+4 ('78704-3845'); DataSift lists want a clean 5-digit."""
    digits = re.sub(r"\D", "", z or "")
    return digits[:5] if len(digits) >= 5 else (z or "").strip()


# Tokens that look like part of a name but aren't: generational suffixes and
# "and others" markers that appear on tax-roll / court-record data.
_SUFFIX_TOKENS = {"JR", "SR", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "ESQ"}
_ETAL_TRAILING_RE = re.compile(r"\s*\b(?:ET\s+AL|ETAL)\.?\s*$", re.IGNORECASE)

# Particles used in compound surnames — when one of these appears mid-name we
# keep it glued to the final word so "Juan De La Cruz" stays ("Juan", "De La Cruz")
# instead of collapsing to ("Juan", "Cruz"). Stored lowercase, compared lowercase.
_SURNAME_PARTICLES = {
    "van", "von", "de", "del", "della", "dela", "di", "du", "la", "le", "les",
    "los", "mac", "mc", "da", "das", "do", "dos", "st", "saint", "el", "al",
    "der", "den", "ter", "ten", "af", "av",
}


def _strip_name_noise(name: str) -> str:
    """Remove ETAL / ET AL / ESQ and trailing generational suffixes from a name."""
    # Drop trailing "ET AL" / "ETAL" (can carry a period).
    name = _ETAL_TRAILING_RE.sub("", name).strip()
    # Peel off trailing suffix tokens one at a time (handles "Smith JR III" etc).
    parts = name.split()
    while parts and parts[-1].rstrip(".").upper() in _SUFFIX_TOKENS:
        parts.pop()
    return " ".join(parts)


def _collapse_middle(parts: list[str]) -> list[str]:
    """Collapse middle tokens into either [first, last] or [first, particle..., last].

    Rules:
      - 1 or 2 tokens → return as-is.
      - 3+ tokens → keep first token and last token. If any middle token is a
        compound-surname particle (Van, De, La, Mc...), glue the run of
        particles + last-token together to preserve "De La Cruz" style names.
    """
    if len(parts) <= 2:
        return parts

    first = parts[0]
    # Find the first compound-particle position in parts[1:-1]; if one exists,
    # the surname starts there and runs through the end.
    for i, tok in enumerate(parts[1:-1], start=1):
        if tok.lower().rstrip(".") in _SURNAME_PARTICLES:
            return [first] + parts[i:]

    # No particle → single-word last name, drop every middle token.
    return [first, parts[-1]]


def _clean_and_split_name(full_name: str) -> tuple[str, str]:
    """Clean a full name for DataSift upload and split into (first, last).

    Handles patterns that cause DataSift "incomplete" records:
    - "LAST, FIRST [MIDDLE]" comma format (probate court records, CAD lookups):
        "Patterson, Rebecca Gayle" → ("Rebecca", "Patterson")
    - Joint names with "&" or "AND": "John & Jane Smith" → ("John", "Smith")
    - Entity names (LLC, Trust, etc.): returns ("", "") — entity goes to Notes
    - "ET AL" / "ETAL" trailing marker → stripped
    - Generational suffixes (JR, SR, II, III, IV, V, ESQ) → stripped
    - Leading initials: "A Lee Rigby" → ("Lee", "Rigby")
    - Multi-word middle names: "Robert Preston Day" → ("Robert", "Day")
    - Compound surnames preserved via particle list (Van, De, La, Mc, St, …)
    - Special characters: strips &, @, #, % from name parts
    """
    if not full_name:
        return ("", "")

    name = full_name.strip()

    # ── Flip "LAST, FIRST MIDDLE" → "FIRST MIDDLE LAST" ──
    # Probate court records and county appraisal lookups frequently store
    # owner names in "Patterson, Rebecca Gayle" format. Without this flip,
    # positional parsing below would treat "Patterson" as first name and
    # "Gayle" as last name (after middle-token collapse drops "Rebecca").
    #
    # Don't flip if the chunk after the comma is just a generational
    # suffix ("Smith, Jr." → drop suffix, keep "Smith") — those are
    # punctuation noise, not name reordering.
    if "," in name and not _is_entity_name(name):
        head, _, tail = name.partition(",")
        head = head.strip()
        tail = tail.strip()
        tail_upper = tail.upper().rstrip(".")
        is_suffix_only = tail_upper in _SUFFIX_TOKENS or tail_upper in {
            "ESQ", "ESQUIRE", "ETAL", "ET AL", "ET. AL.", "JR.", "SR.", "II.", "III.", "IV.",
        }
        if head and tail and not is_suffix_only:
            name = f"{tail} {head}"

    # Entity names → empty (don't put business names in person fields)
    if _is_entity_name(name):
        return ("", "")

    # Split joint owners on " & " or " AND " — keep first person only.
    # The first person may or may not already carry the surname; the trailing
    # "& OTHER L LASTNAME" form (TX tax roll: "JULIUS W LAWSON & EDNA L"
    # post-flip becomes "JULIUS W & EDNA L LAWSON") puts the shared last
    # name only at the very end, so we have to detect when the first chunk
    # lacks a real last name and pull the trailing one in.
    joint_match = re.split(r"\s+(?:&|AND)\s+", name, maxsplit=1, flags=re.IGNORECASE)
    if len(joint_match) > 1:
        first_person = joint_match[0].strip()
        second_part = joint_match[1].strip()
        second_words = second_part.split()
        first_words = first_person.split()
        last_name = second_words[-1] if second_words else ""

        def _has_real_surname(words: list[str]) -> bool:
            """A 'real' surname is the last token when it isn't a single
            letter, isn't a generational suffix, and isn't ETAL noise."""
            if len(words) < 2:
                return False
            tail = words[-1].rstrip(".").upper()
            if len(tail) <= 1:
                return False
            if tail in _SUFFIX_TOKENS:
                return False
            if tail in {"ETAL", "ET", "AL", "ESQ"}:
                return False
            return True

        if _has_real_surname(first_words):
            # First person already has its own surname (e.g., "John Smith
            # & Jane Doe"). Drop the second person; preserve first as-is.
            name = first_person
        elif last_name:
            # First person lacks a real surname — appended shared one wins.
            # "John & Jane Smith" → "John Smith"
            # "Julius W & Edna L Lawson" → "Julius W Lawson"
            name = f"{first_person} {last_name}"
        else:
            name = first_person

    # Strip ET AL and generational suffixes before any splitting.
    name = _strip_name_noise(name)

    # Strip special chars and trailing punctuation, collapse whitespace.
    # Trailing comma/period leak from upstream truncation patterns
    # (e.g. MVBA bid sheet "DONALD ZEDLER, ..." → "Donald Zedler,").
    name = re.sub(r"[&@#%]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Strip per-token trailing punctuation that survived special-char filtering.
    name = " ".join(t.rstrip(",.;:") for t in name.split() if t.rstrip(",.;:"))

    if not name:
        return ("", "")

    parts = name.split()

    # Drop leading single-letter initial ("A Lee Rigby" → "Lee Rigby",
    # "D. Bruce Kruger" → "Bruce Kruger"). Track it so we know the survivor
    # is positionally a last-name when only one token remains.
    leading_initial_stripped = False
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]\.?", parts[0]):
        parts = parts[1:]
        leading_initial_stripped = True

    # Drop trailing single-letter initial ("Alton D" → "Alton").
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]\.?", parts[-1]):
        parts = parts[:-1]

    # Drop any remaining single-letter middle-initial tokens.
    if len(parts) >= 3:
        parts = [parts[0]] + [p for p in parts[1:-1] if not re.fullmatch(r"[A-Za-z]\.?", p)] + [parts[-1]]

    # Collapse multi-word middle names while preserving compound surnames.
    parts = _collapse_middle(parts)

    if not parts:
        return ("", "")
    if len(parts) == 1:
        token = parts[0]
        if len(token) <= 2:  # Looked like a bare initial — drop it entirely
            return ("", "")
        # If a leading initial was stripped, the survivor was positioned as the
        # middle/last element of the original name → treat as last name.
        # Otherwise the survivor was the first word (e.g., "CHESTER III" after
        # stripping the suffix) → treat as first name.
        return ("", token) if leading_initial_stripped else (token, "")
    return (parts[0], " ".join(parts[1:]))


def _split_name(full_name: str) -> tuple[str, str, str]:
    """Split full name into (first, last, entity_type).

    Delegates to _clean_and_split_name() for DataSift-specific spouse /
    middle-initial handling, then layers entity detection on top.
    entity_type is one of "government" | "business" | "" (person).
    """
    from notice_parser import _detect_entity_type
    entity_type = _detect_entity_type(full_name)
    if entity_type:
        return ("", "", entity_type)
    first, last = _clean_and_split_name(full_name)
    return (first, last, "")


# Map notice_type → DataSift list name for niche sequential marketing.
# DataSift auto-creates lists from CSV if they don't exist yet.
# notice_type → DataSift LIST name. These MUST match the account's existing
# built-in list titles (not SiftStack's internal concept names) so records
# land on the right list and the Sold -> Reset / cleanup sequences — which
# act on list titles — actually fire. code_violation → "Code Enforcement"
# and lien → "Liens" are the account's default list names; "Tax Sale" has no
# built-in equivalent and stays its own SiftStack list (by design).
NOTICE_TYPE_TO_LIST = {
    "foreclosure": "Foreclosure",
    "probate": "Probate",
    "tax_sale": "Tax Sale",
    "tax_delinquent": "Tax Delinquent",
    "eviction": "Eviction",
    "code_violation": "Code Enforcement",
    "divorce": "Divorce",
    "lien": "Liens",
    # No built-in DataSift list equivalent — SiftStack-only list (like Tax Sale);
    # DataSift auto-creates it from the CSV on first upload.
    "lis_pendens": "Lis Pendens",
    "fire_damage": "Fire Damage",
}


def _build_tags(notice: NoticeData) -> str:
    """Build comma-separated tags string for DataSift upload.

    Tags include:
    - Courthouse Data (all records — for niche sequential filter presets)
    - notice_type (foreclosure, tax_sale, probate, tax_delinquent)
    - county (travis, bell, williamson)
    - YYYY-MM date tag
    - deceased/living status
    - DM confidence level (for deceased records)
    - has_auction if auction date is upcoming
    """
    # Sold (dropped-off-roll) record: minimal tag set so DataSift's "Sold
    # Property Cleanup" sequence fires (trigger = Property Tags Added "Sold").
    # Capital-S "Sold" matches the sequence condition exactly. No other tags —
    # this is a tag-update row matched to an existing record by address.
    if notice.record_status == "sold":
        sold_tags = ["Sold"]
        if notice.county:
            sold_tags.append(notice.county.lower())
        if notice.date_added:
            try:
                dt = datetime.strptime(notice.date_added, "%Y-%m-%d")
                sold_tags.append(f"sold_{dt.strftime('%Y-%m')}")
            except ValueError:
                pass
        return ",".join(sold_tags)

    # Resolved code-enforcement case: a scoped tag-update row. "Code Violation
    # Resolved" is the trigger for the "Code Violation Cleanup" sequence, which
    # removes ONLY the "Code Enforcement" list — this is NOT a Sold, so other
    # distress signals on the same property (probate/tax/lien) survive.
    if notice.record_status == "resolved":
        res_tags = ["Code Violation Resolved"]
        if notice.county:
            res_tags.append(notice.county.lower())
        if notice.date_added:
            try:
                dt = datetime.strptime(notice.date_added, "%Y-%m-%d")
                res_tags.append(f"resolved_{dt.strftime('%Y-%m')}")
            except ValueError:
                pass
        return ",".join(res_tags)

    tags = ["Courthouse Data"]

    # Notice type — use the DataSift display name (e.g. "Foreclosure",
    # "Probate", "Code Enforcement") so the tag reads cleanly and matches the
    # account's list titles. Falls back to a title-cased form for any type not
    # in the map.
    if notice.notice_type:
        tags.append(
            NOTICE_TYPE_TO_LIST.get(
                notice.notice_type,
                notice.notice_type.replace("_", " ").title(),
            )
        )

    # Specific lien type (e.g. "abstract_of_judgment") for finer filter presets
    if notice.lien_type:
        tags.append(notice.lien_type.lower().replace("'", "").replace(" ", "_"))

    # County
    if notice.county:
        tags.append(notice.county.lower())

    # Month tag from date_added
    if notice.date_added:
        try:
            dt = datetime.strptime(notice.date_added, "%Y-%m-%d")
            tags.append(dt.strftime("%Y-%m"))
        except ValueError:
            pass

    # Deceased/living status
    if notice.owner_deceased == "yes":
        tags.append("deceased")
        # DM confidence
        if notice.dm_confidence:
            tags.append(f"{notice.dm_confidence}_confidence")
    else:
        tags.append("living")

    # Upcoming auction
    if notice.auction_date:
        try:
            auction_dt = datetime.strptime(notice.auction_date, "%Y-%m-%d")
            if auction_dt >= datetime.now():
                tags.append("has_auction")
        except ValueError:
            pass

    # Tax delinquent flag
    if notice.tax_delinquent_amount:
        try:
            amt = float(notice.tax_delinquent_amount)
            if amt > 0:
                tags.append("tax_delinquent")
        except (ValueError, TypeError):
            pass

    # Deep prospecting tags
    if notice.decision_maker_status == "verified_living":
        tags.append("dm_verified")
    if notice.heir_map_json:
        tags.append("has_heirs")
    elif notice.owner_deceased == "yes":
        tags.append("no_heirs")
    if (notice.owner_deceased == "yes"
            and notice.decision_maker_street
            and notice.decision_maker_street != notice.address):
        tags.append("has_dm_address")

    # Signing chain tags
    if notice.signing_chain_count:
        try:
            sc_count = int(notice.signing_chain_count)
            tags.append(f"signing_chain_{sc_count}")
            # Check if all signing heirs have phone data
            if notice.heir_map_json:
                import json as _json
                try:
                    heirs = _json.loads(notice.heir_map_json)
                    signers = [h for h in heirs
                               if h.get("signing_authority") and h.get("status") != "deceased"]
                    traced = [h for h in signers if h.get("phones")]
                    # DM #1 counts as traced if notice has primary_phone
                    if notice.primary_phone and signers:
                        dm1_name = (notice.decision_maker_name or "").lower()
                        if any(h.get("name", "").lower() == dm1_name for h in signers):
                            traced_names = {h.get("name", "").lower() for h in traced}
                            if dm1_name not in traced_names:
                                traced.append({"name": dm1_name})  # count DM #1
                    if traced and len(traced) >= len(signers):
                        tags.append("signing_chain_complete")
                    elif traced:
                        tags.append("signing_chain_partial")
                except (ValueError, TypeError):
                    pass
        except (ValueError, TypeError):
            pass

    # Entity research tags
    if notice.entity_type:
        tags.append("entity_owned")
        if notice.entity_person_name:
            tags.append("entity_researched")

    # Photo import tag (source_url starts with "photo:")
    if notice.source_url and notice.source_url.startswith("photo:"):
        tags.append("photo_import")

    return ",".join(tags)


def _get_contact_info(notice: NoticeData) -> dict:
    """Determine the contact person and mailing address.

    For deceased owners with a decision maker: contact = DM
    For living owners: contact = property owner
    For entity-owned properties: try tax_owner_name or DM as real person fallback

    Mailing address always falls back to property address to avoid DataSift
    marking records as incomplete.
    """
    if notice.owner_deceased == "yes" and notice.decision_maker_name:
        first, last, _et = _split_name(notice.decision_maker_name)
        # Fall back to property address when DM has no mailing address
        street = notice.decision_maker_street or notice.address
        city = notice.decision_maker_city or notice.city
        state = notice.decision_maker_state or notice.state
        zip_code = notice.decision_maker_zip or notice.zip
        return {
            "first": first,
            "last": last,
            "business": "",
            "street": street,
            "city": city,
            "state": state,
            "zip": zip_code,
        }

    # Living owner. Split into a person (First/Last) and/or a business (Business
    # Name): an LLC/trust/government owner routes to Business Name, an individual
    # to First/Last. When a real person is found *behind* an entity (entity
    # research agent, individual on the tax roll), we keep BOTH.
    business = ""
    first, last, entity_type = _split_name(notice.owner_name)
    if entity_type:  # owner_name itself is a business/government entity
        business = notice.owner_name.strip()

    if not first and not last:
        # Try entity research result (signing member, registered agent, etc.)
        if notice.entity_person_name:
            first, last, _et = _split_name(notice.entity_person_name)
        # Try tax-roll owner. tax_owner_name is LAST-FIRST (NAMELF): normalize to
        # FIRST LAST before splitting (else "JEFFEIS TANISA" reverses and
        # "MCDONALD ROBERT J & MEGAN L" collapses to Last=J). If the tax owner is
        # itself an entity, route it to Business Name instead of a person field.
        if not first and not last and notice.tax_owner_name.strip():
            if not _is_entity_name(notice.tax_owner_name):
                from obituary_enricher import parse_tax_owner_name
                _variants = parse_tax_owner_name(notice.tax_owner_name)
                # NEVER fall back to the raw tax_owner_name here. It is stored in
                # LAST-FIRST (NAMELF) order; splitting it positionally reverses the
                # name ("SPENCER KIMBERLY ANN" → First=Spencer, Last=Ann) and can
                # drop the real first name entirely. A reversed name is worse than
                # no name — it silently ships wrong contacts to marketing — so when
                # the NAMELF parser can't resolve a person, leave the person fields
                # blank and let the entity/DM fallbacks below handle it.
                if _variants:
                    first, last, _et = _split_name(_variants[0])
                elif not business:
                    # Unparseable as a person AND we can't trust the word order.
                    # In practice these are organisations `_is_entity_name` misses
                    # (churches, housing authorities, rental cos: "MT ZION BAPTIST
                    # CHURCH" → First=Mt/Last=Church). Preserve the value in
                    # Business Name so the record still carries an owner — just
                    # never as a fabricated person.
                    business = notice.tax_owner_name.strip()
            elif not business:
                business = notice.tax_owner_name.strip()
        # Try decision maker (probate PR, etc.)
        if not first and not last and notice.decision_maker_name:
            first, last, _et = _split_name(notice.decision_maker_name)

    # Tidy the business display: drop a trailing "ET AL", title-case.
    if business:
        business = _ETAL_TRAILING_RE.sub("", business).strip().title()

    # Mailing is ALWAYS populated — the owner's tax-roll mailing when known,
    # otherwise the property address (never left blank, even when identical).
    street = notice.owner_street or notice.address
    city = notice.owner_city or notice.city
    state = notice.owner_state or notice.state
    zip_code = notice.owner_zip or notice.zip
    return {
        "first": first,
        "last": last,
        "business": business,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
    }


def _build_heir_summary(notice: NoticeData) -> str:
    """Build signing chain + family summary from heir_map_json.

    Two sections:
    1. SIGNING CHAIN — heirs with signing_authority who must sign to sell property.
       Includes phone + address for each.
    2. OTHER FAMILY — everyone else (in-laws, step-children, etc.) in compact format.
    """
    if not notice.heir_map_json:
        return ""

    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not heirs:
        return ""

    # Split into signing chain vs others
    signers = [h for h in heirs
                if h.get("signing_authority") and h.get("status") != "deceased"]
    non_signers = [h for h in heirs if not h.get("signing_authority") or h.get("status") == "deceased"]

    lines = []

    # ── Signing chain section ──
    if signers:
        lines.append(f"=== SIGNING CHAIN ({len(signers)} heir{'s' if len(signers) != 1 else ''} must sign) ===")
        for i, h in enumerate(signers, 1):
            name = h.get("name", "?")
            rel = h.get("relationship", "unknown")
            status = h.get("status", "unverified")
            if h.get("verification_skipped"):
                status_label = "NOT VERIFIED (reference only)"
            else:
                status_label = "ALIVE" if status == "verified_living" else status.upper()

            # Phone info
            phones = h.get("phones", [])
            # DM #1 phones are on flat NoticeData fields, not in heir_map_json
            if not phones and notice.primary_phone:
                dm1_name = (notice.decision_maker_name or "").strip().lower()
                if name.lower() == dm1_name:
                    phones = [notice.primary_phone]

            phone_str = phones[0] if phones else "no phone yet"
            lines.append(f"{i}. {name} ({rel}) — {status_label} — {phone_str}")

            # Address
            street = h.get("street", "")
            if street:
                city = h.get("city", "")
                state = h.get("state", "TX")
                zip_code = h.get("zip", "")
                addr_parts = [street]
                if city:
                    addr_parts.append(city)
                addr_parts.append(f"{state} {zip_code}".strip())
                lines.append(f"   Mail: {', '.join(addr_parts)}")
    else:
        lines.append("=== NO SIGNING CHAIN IDENTIFIED ===")

    # ── Non-signing family section ──
    # List EVERY non-signing survivor (no truncation). Large obituaries get their
    # peripheral relatives captured here for reference even though we don't chase
    # them — including the survivors intentionally left unverified to bound LLM
    # cost (flagged "reference only"; see obituary_enricher heir-verification cap).
    if non_signers:
        entries = []
        for h in non_signers:
            name = h.get("name", "?")
            rel = h.get("relationship", "")
            status = h.get("status", "unverified")
            if h.get("verification_skipped"):
                tag = "reference only — not verified"
            elif status == "verified_living":
                tag = "living"
            elif status == "deceased":
                tag = "deceased"
            else:
                tag = status
            entries.append(f"{name} ({rel}) [{tag}]")
        lines.append("")
        lines.append(f"=== OTHER FAMILY / SURVIVORS ({len(entries)}) ===")
        lines.append(", ".join(entries))

    return "\n".join(lines)


def _build_dm_section(notice: NoticeData) -> str:
    """Build ranked decision maker section with status and address."""
    dms = []

    for i, (name_attr, rel_attr, status_attr) in enumerate([
        ("decision_maker_name", "decision_maker_relationship", "decision_maker_status"),
        ("decision_maker_2_name", "decision_maker_2_relationship", "decision_maker_2_status"),
        ("decision_maker_3_name", "decision_maker_3_relationship", "decision_maker_3_status"),
    ], 1):
        name = getattr(notice, name_attr, "")
        if not name:
            continue
        rel = getattr(notice, rel_attr, "") or "unknown"
        status = getattr(notice, status_attr, "") or "unverified"

        status_label = "VERIFIED LIVING" if status == "verified_living" else status
        line = f"{i}. {name} ({rel}) — {status_label}"

        # Include DM1 mailing address if available
        if i == 1 and notice.decision_maker_street:
            addr_parts = [notice.decision_maker_street]
            if notice.decision_maker_city:
                addr_parts.append(notice.decision_maker_city)
            if notice.decision_maker_state:
                addr_parts.append(notice.decision_maker_state)
            if notice.decision_maker_zip:
                addr_parts[-1] = addr_parts[-1] + " " + notice.decision_maker_zip
            line += f"\n   Mail: {', '.join(addr_parts)}"

        dms.append(line)

    if not dms:
        return ""

    return "=== DECISION MAKERS ===\n" + "\n".join(dms)


_DM_RELATIONSHIP_LABELS = {
    "care_of_caretaker":  "care-of caretaker",
    "joint_owner":        "joint owner",
    "obituary_survivor":  "surviving family member",
    "spouse":             "spouse",
    "child":              "child",
    "executor":           "executor",
    "trustee":            "trustee",
}

_DM_SOURCE_LABELS = {
    "tax_record_c_o":         "BellCAD tax record (C/O)",
    "tax_record_joint_owner": "county tax record",
    "obituary_survivors":     "obituary search",
    "snippet":                "obituary snippet",
}


def _build_caretaker_section(notice: NoticeData) -> str:
    """RAWCALL: surface the live decision-maker for non-deceased records.

    Tax-delinquent records routinely encode a C/O caretaker (the person who
    actually handles the property's mail) — typically because the legal owner
    is elderly, absentee, or in a trust. Without this section the DataSift
    Notes column shows only the property summary and the C/O name is buried
    in the (rarely surfaced) decision-maker columns.

    Returns "" when there's no DM, or when the owner is deceased — the
    deceased branch of `_build_notes()` already emits a multi-section
    `=== DECISION MAKERS ===` block via `_build_dm_section()`, so we don't
    duplicate.
    """
    if notice.owner_deceased == "yes":
        return ""
    name = (notice.decision_maker_name or "").strip()
    if not name:
        return ""

    rel_raw = (notice.dm_relationship or notice.decision_maker_relationship or "").strip()
    rel_label = _DM_RELATIONSHIP_LABELS.get(rel_raw, rel_raw or "live decision-maker")

    src_raw = (notice.decision_maker_source or "").strip()
    src_label = _DM_SOURCE_LABELS.get(src_raw, src_raw)

    header_attr = "from " + src_label if src_label else "live contact"

    lines = [
        "=== CONTACT FIRST ===",
        f"{name} ({rel_label}, {header_attr})",
    ]

    # Optional caretaker mailing address — saves a skip-trace step when known.
    addr = (notice.decision_maker_street or "").strip()
    if addr:
        addr_parts = [addr]
        if notice.decision_maker_city:
            addr_parts.append(notice.decision_maker_city)
        if notice.decision_maker_state:
            tail = notice.decision_maker_state
            if notice.decision_maker_zip:
                tail = f"{tail} {notice.decision_maker_zip}"
            addr_parts.append(tail)
        lines.append(f"Mail: {', '.join(addr_parts)}")

    # Action language — this is the headline of the section. Tailor to C/O.
    if rel_raw == "care_of_caretaker":
        action = (
            "Action: Skip-trace this name first → call them. They handle this "
            "property's mail and likely make decisions for the legal owner."
        )
    else:
        action = (
            "Action: Skip-trace and contact this person — they're the live "
            "decision-maker for this property."
        )
    lines.append(action)

    return "\n".join(lines)


def _build_property_section(notice: NoticeData) -> str:
    """Build the property/notice details section for Notes."""
    parts = []

    # Include entity name when owner is LLC/Trust (name stripped from contact fields)
    if notice.owner_name and _is_entity_name(notice.owner_name):
        parts.append(f"Entity: {notice.owner_name}")

    # Include entity research contact if found
    if notice.entity_person_name:
        role = notice.entity_person_role.replace("_", " ").title() if notice.entity_person_role else "Unknown"
        parts.append(f"Entity Contact: {notice.entity_person_name} ({role})")

    if notice.notice_type:
        parts.append(notice.notice_type.replace("_", " ").title())

    # Lien / lis-pendens context: WHY the owner (our lead) is distressed.
    # Lis pendens reuses lien_creditor to carry the PLAINTIFF who filed the suit
    # (the suit type is always "Lis Pendens", already shown via the notice_type
    # line above, so lien_type is left unset for these).
    if notice.notice_type == "lis_pendens":
        if notice.lien_creditor:
            parts.append(f"Plaintiff: {notice.lien_creditor}")
    else:
        if notice.lien_type:
            parts.append(f"Lien: {notice.lien_type}")
        if notice.lien_creditor:
            parts.append(f"Creditor: {notice.lien_creditor}")

    if notice.violation_description:
        parts.append(f"Violation: {notice.violation_description}")

    if notice.compliance_deadline:
        parts.append(f"Comply By: {_format_date(notice.compliance_deadline)}")

    if notice.auction_date:
        parts.append(f"Auction: {_format_date(notice.auction_date)}")

    if notice.tax_delinquent_amount:
        tax_str = f"Tax Due: ${notice.tax_delinquent_amount}"
        if notice.tax_delinquent_years:
            tax_str += f" ({notice.tax_delinquent_years} yrs)"
        parts.append(tax_str)

    if notice.source_url:
        parts.append(f"Source: {notice.source_url}")

    return " | ".join(parts)


def _build_legal_owner_section(notice: NoticeData) -> str:
    """Surface the pristine county-record owner name in Notes for deep
    prospecting. Only emits when tax_owner_name is populated AND meaningfully
    differs from the cleaned owner_name (catches ETAL / JR / III / trust
    markers that _clean_and_split_name strips for the First/Last columns)."""
    raw = (notice.tax_owner_name or "").strip()
    if not raw:
        return ""
    # Skip when the raw value is just a case-folded version of the display name.
    if raw.lower() == (notice.owner_name or "").lower().strip():
        return ""
    return f"=== LEGAL OWNER (COUNTY RECORD) ===\n{raw}"


def _build_notes(notice: NoticeData) -> str:
    """Build a structured notes string for DataSift records.

    Deceased records get a multi-section format with heir map and DM summary.
    Living records get a simpler single-section format.
    """
    if notice.owner_deceased == "yes":
        sections = []

        # Section 1: Deceased owner header
        deceased_parts = []
        if notice.decedent_name:
            deceased_parts.append(f"Decedent: {notice.decedent_name}")
        if notice.date_of_death:
            deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
        if notice.obituary_url:
            deceased_parts.append(f"Obituary: {notice.obituary_url}")

        confidence_line = ""
        if notice.dm_confidence:
            confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
            if notice.dm_confidence_reason:
                confidence_line += f" — {notice.dm_confidence_reason}"

        if deceased_parts or confidence_line:
            header = "=== DECEASED OWNER ==="
            body = " | ".join(deceased_parts)
            if confidence_line:
                body += f"\n{confidence_line}" if body else confidence_line
            sections.append(f"{header}\n{body}")

        # Section 2: Decision makers
        dm_section = _build_dm_section(notice)
        if dm_section:
            sections.append(dm_section)

        # Section 3: Heir map
        heir_section = _build_heir_summary(notice)
        if heir_section:
            sections.append(heir_section)

        # Section 4: Property/notice details
        prop_section = _build_property_section(notice)
        if prop_section:
            sections.append(f"=== PROPERTY ===\n{prop_section}")

        # Section 5: pristine county-record owner name (deep-prospecting signal)
        legal_section = _build_legal_owner_section(notice)
        if legal_section:
            sections.append(legal_section)

        if notice.report_url:
            sections.append(f"=== REPORT ===\n{notice.report_url}")

        return "\n\n".join(sections)

    # Living owner — caretaker (RAWCALL) on top, then property + optional legal owner
    parts = []
    caretaker_section = _build_caretaker_section(notice)
    if caretaker_section:
        parts.append(caretaker_section)
    parts.append(_build_property_section(notice))
    legal_section = _build_legal_owner_section(notice)
    if legal_section:
        parts.append(legal_section)
    return "\n\n".join(p for p in parts if p)


def _build_dm_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 1: deceased owner header + DM breakdown + property.

    For living records, returns the simple property section (plus optional
    legal-owner section when raw county name differs from cleaned owner_name).
    Used by write_datasift_split_csvs() for the DMs upload.
    """
    if notice.owner_deceased != "yes":
        parts = []
        caretaker_section = _build_caretaker_section(notice)
        if caretaker_section:
            parts.append(caretaker_section)
        parts.append(_build_property_section(notice))
        legal_section = _build_legal_owner_section(notice)
        if legal_section:
            parts.append(legal_section)
        return "\n\n".join(p for p in parts if p)

    sections = []

    # Deceased owner header
    deceased_parts = []
    if notice.decedent_name:
        deceased_parts.append(f"Decedent: {notice.decedent_name}")
    if notice.date_of_death:
        deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
    if notice.obituary_url:
        deceased_parts.append(f"Obituary: {notice.obituary_url}")

    confidence_line = ""
    if notice.dm_confidence:
        confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
        if notice.dm_confidence_reason:
            confidence_line += f" — {notice.dm_confidence_reason}"

    if deceased_parts or confidence_line:
        header = "=== DECEASED OWNER ==="
        body = " | ".join(deceased_parts)
        if confidence_line:
            body += f"\n{confidence_line}" if body else confidence_line
        sections.append(f"{header}\n{body}")

    # Decision makers
    dm_section = _build_dm_section(notice)
    if dm_section:
        sections.append(dm_section)

    # Property details
    prop_section = _build_property_section(notice)
    if prop_section:
        sections.append(f"=== PROPERTY ===\n{prop_section}")

    # Pristine county-record owner name (deep-prospecting signal)
    legal_section = _build_legal_owner_section(notice)
    if legal_section:
        sections.append(legal_section)

    return "\n\n".join(sections)


def _build_heir_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 2: full heir map only.

    Used by write_datasift_split_csvs() for the Heirs upload.
    Returns empty string if no heir data.
    """
    return _build_heir_summary(notice)


def _validate_row(row: dict) -> tuple[bool, list[str]]:
    """Check a row dict for DataSift completeness.

    DataSift marks records incomplete when missing owner first/last name,
    mailing address, or property address.

    Returns:
        (is_complete, issues) — True if record will be "clean" in DataSift.
    """
    issues = []
    # A Business Name satisfies the owner-identity requirement (entity-owned
    # property mailed to the company); otherwise we need a person first+last.
    if not row.get("Business Name"):
        if not row.get("Owner First Name"):
            issues.append("no_first_name")
        if not row.get("Owner Last Name"):
            issues.append("no_last_name")
    if not row.get("Property Street Address"):
        issues.append("no_property_address")
    if not row.get("Mailing Street Address"):
        issues.append("no_mailing_address")
    return (len(issues) == 0, issues)


def clean_datasift_rows(rows: list[dict], label: str = "") -> tuple[list[dict], dict]:
    """Final audit + cleanup pass on built rows, run before a list is delivered.

    Guarantees the CSV is clean regardless of upstream messiness:
      - de-duplicates by Parcel ID (falling back to a house-numbered Property
        address+ZIP) so the same property never ships twice, while distinct
        vacant-land parcels that share a street name are preserved;
      - strips trailing punctuation and collapses whitespace in addresses;
      - guarantees 5-digit ZIPs.

    Returns (cleaned_rows, stats) and logs a one-line audit summary.
    """
    stats = {"in": len(rows), "dup_removed": 0, "addr_cleaned": 0, "zip_fixed": 0}
    seen: set = set()
    out: list[dict] = []
    for r in rows:
        pid = (r.get("Parcel ID", "") or "").strip()
        if pid:
            key = ("p", pid.upper())
        else:
            addr = (r.get("Property Street Address", "") or "").strip().upper()
            if addr[:1].isdigit():
                key = ("a", re.sub(r"\s+", " ", addr), _zip5(r.get("Property ZIP Code", "")))
            else:
                key = ("u", id(r))  # vacant / no-parcel — never merge distinct parcels
        if key[0] != "u" and key in seen:
            stats["dup_removed"] += 1
            continue
        seen.add(key)
        for col in ("Property Street Address", "Mailing Street Address"):
            v = r.get(col, "") or ""
            nv = re.sub(r"\s+", " ", v).strip().rstrip(".,").strip()
            if nv != v:
                r[col] = nv
                stats["addr_cleaned"] += 1
        for col in ("Property ZIP Code", "Mailing ZIP Code"):
            v = r.get(col, "") or ""
            nv = _zip5(v)
            if nv != v:
                r[col] = nv
                stats["zip_fixed"] += 1
        out.append(r)
    stats["out"] = len(out)
    if stats["dup_removed"] or stats["addr_cleaned"] or stats["zip_fixed"]:
        logger.info(
            "  audit/clean%s: %d→%d rows (dedup -%d, addr %d, zip %d)",
            f" [{label}]" if label else "", stats["in"], stats["out"],
            stats["dup_removed"], stats["addr_cleaned"], stats["zip_fixed"],
        )
    return out, stats


def _build_row(notice: NoticeData, notes_override: str | None = None) -> dict:
    """Build a single CSV row dict for a NoticeData record.

    Args:
        notice: The notice to format.
        notes_override: If provided, use this as the Notes value instead of
            calling _build_notes(). Used by write_datasift_split_csvs().

    Returns:
        Dict keyed by DATASIFT_COLUMNS headers.
    """
    contact = _get_contact_info(notice)
    tags = _build_tags(notice)
    if notice.record_status == "sold":
        # Don't re-add a sold parcel to the Tax Delinquent list — the cleanup
        # sequence removes it from lists. Tax/value fields stay blank so the
        # upload doesn't overwrite the record's delinquency history. The sold
        # note always wins over any notes_override (these are tag-update rows).
        list_name = ""
        notes = (
            f"Dropped off {notice.county} tax-delinquent roll on "
            f"{_format_date(notice.date_added)} — likely paid off or sold."
        )
    elif notice.record_status == "resolved":
        # Scoped cleanup: blank Lists so we don't re-add to the "Code
        # Enforcement" list — the "Code Violation Cleanup" sequence removes only
        # that list. Value/tax fields stay blank so this tag-update row doesn't
        # overwrite the record. The resolution note always wins over notes_override.
        list_name = ""
        detail = notice.resolution_note or "resolved"
        case_ref = f" {notice.case_id}" if notice.case_id else ""
        notes = (
            f"Code-enforcement case{case_ref} resolved ({detail}) — "
            "removed from Code Enforcement marketing."
        )
    else:
        list_name = NOTICE_TYPE_TO_LIST.get(notice.notice_type, "")
        notes = notes_override if notes_override is not None else _build_notes(notice)

    # Conditionally map auction_date to the right built-in field
    tax_auction = ""
    foreclosure_date = ""
    probate_open = ""
    if notice.notice_type == "tax_sale":
        tax_auction = _format_date(notice.auction_date)
    elif notice.notice_type == "foreclosure":
        foreclosure_date = _format_date(notice.auction_date)
    elif notice.notice_type == "probate":
        probate_open = _format_date(notice.date_added)

    # Personal Representative only for probate notices
    personal_rep = ""
    if notice.notice_type == "probate" and notice.decision_maker_name:
        personal_rep = notice.decision_maker_name

    return {
        # ── Core auto-mapped ──
        "Property Street Address": notice.address,
        "Property City": notice.city,
        "Property State": notice.state or "TX",
        "Property ZIP Code": _zip5(notice.zip),
        "Business Name": contact.get("business", ""),
        "Owner First Name": contact["first"],
        "Owner Last Name": contact["last"],
        "Mailing Street Address": contact["street"],
        "Mailing City": contact["city"],
        "Mailing State": contact["state"],
        "Mailing ZIP Code": _zip5(contact["zip"]),
        # ── Phone/Email (Tracerfy → DataSift generic Phone N format) ──
        "Phone 1": notice.primary_phone,
        "Phone 2": notice.mobile_1,
        "Phone 3": notice.mobile_2,
        "Phone 4": notice.mobile_3,
        "Phone 5": notice.mobile_4,
        "Phone 6": notice.mobile_5,
        "Phone 7": notice.landline_1,
        "Phone 8": notice.landline_2,
        "Phone 9": notice.landline_3,
        "Email 1": notice.email_1,
        "Email 2": notice.email_2,
        "Email 3": notice.email_3,
        "Email 4": notice.email_4,
        "Email 5": notice.email_5,
        "Tags": tags,
        "Lists": list_name,
        "Notes": notes,
        # ── Built-in fields ──
        "Estimated Value": notice.estimated_value,
        "MSL Status": notice.mls_status,
        "Last Sale Date": _format_date(notice.mls_last_sold_date),
        "Last Sale Price": notice.mls_last_sold_price,
        "Equity Percentage": notice.equity_percent,
        "Tax Deliquent Value": notice.tax_delinquent_amount,
        "Tax Delinquent Year": notice.tax_delinquent_years,
        "Tax Auction Date": tax_auction,
        "Foreclosure Date": foreclosure_date,
        "Probate Open Date": probate_open,
        "Personal Representative": personal_rep,
        "Parcel ID": notice.parcel_id,
        "Structure Type": notice.property_type,
        "Year Built": notice.year_built,
        "Living SqFt": notice.sqft,
        "Bedrooms": notice.bedrooms,
        "Bathrooms": notice.bathrooms,
        "Lot (Acres)": notice.lot_size,
        # ── Custom fields ("Deceased & Heir Intelligence" group) ──
        # Headers match the custom-field labels 1:1; select values must be
        # exact option labels (case-sensitive match on import).
        "Notice Type": _select_label(notice.notice_type, _NOTICE_TYPE_LABELS),
        "County": _county_label(notice.county),
        "Date Added": _format_date(notice.date_added),
        "Owner Deceased": _select_label(notice.owner_deceased, _OWNER_DECEASED_LABELS),
        "Date of Death": notice.date_of_death,
        "Decedent Name": notice.decedent_name,
        "Decision Maker (Name)": notice.decision_maker_name,
        "DM Relationship": notice.decision_maker_relationship,
        "Decision-Maker Confidence": _select_label(notice.dm_confidence, _CONFIDENCE_LABELS),
        "DM 2 Name / Relationship": _name_rel(
            notice.decision_maker_2_name, notice.decision_maker_2_relationship),
        "DM 3 Name / Relationship": _name_rel(
            notice.decision_maker_3_name, notice.decision_maker_3_relationship),
        "Obituary URL": notice.obituary_url,
        "Source URL": notice.source_url,
        # ── Deep prospecting fields ──
        "Decision-Maker Status": _select_label(notice.decision_maker_status, _DM_STATUS_LABELS),
        "DM Source": notice.decision_maker_source,
        "DM 2 Status": _select_label(notice.decision_maker_2_status, _DM_STATUS_LABELS),
        "DM 3 Status": _select_label(notice.decision_maker_3_status, _DM_STATUS_LABELS),
        "Heir Count": _heir_count(notice),
        "Heirs Living": notice.heirs_verified_living,
        "Signatures to Close": notice.signing_chain_count,
        "Signing Chain Names": notice.signing_chain_names,
        "DM Confidence Reason": notice.dm_confidence_reason,
        "Data Flags": notice.missing_data_flags,
        "Title Flag": _title_flag(notice.missing_data_flags),
        # ── Entity research fields ──
        "Entity Type": notice.entity_type,
        "Entity Contact + Role": _name_rel(
            notice.entity_person_name, notice.entity_person_role),
    }


_KNOWN_BAD_CITY_ZIP = {
    # (Title-cased city, 5-digit ZIP) pairs that are definitely wrong for our
    # target counties. Travis 78xxx ZIPs misrouted to Dallas, McKinney, etc.
    ("Dallas", "78738"),
    ("Dallas", "78731"),
    ("McKinney", "78645"),
    ("Camden", "78731"),
    ("Wolfforth", "78734"),
    ("Kerrville", "78731"),
    ("Miami Beach", "78704"),
    ("Gainesville", "78620"),
    ("Orange", "78641"),
    ("Troup", "78653"),
    ("Denver", "78736"),
}


def _check_city_zip(notice: NoticeData) -> None:
    """Tag city_zip_mismatch flag if (city, zip) is on the known-bad list."""
    if not notice.city or not notice.zip:
        return
    pair = (notice.city.title(), notice.zip[:5])
    if pair in _KNOWN_BAD_CITY_ZIP:
        existing = (notice.missing_data_flags or "").split("|")
        if "city_zip_mismatch" not in existing:
            notice.missing_data_flags = "|".join(
                p for p in existing + ["city_zip_mismatch"] if p
            )


def _is_government_owner(notice: NoticeData) -> bool:
    """Return True when the owner string is a government/municipal entity."""
    from notice_parser import _detect_entity_type
    name = notice.owner_name or notice.tax_owner_name or ""
    return _detect_entity_type(name) == "government"


def write_datasift_csv(
    notices: list[NoticeData],
    filename: str | None = None,
    keep_government: bool = False,
) -> Path:
    """Write notices to a DataSift-formatted CSV file.

    Government-owned records (Travis County Trustee, City Of Lakeway, etc.)
    are dropped by default since they aren't investable. Pass
    keep_government=True to include them anyway (debugging / audit).

    Args:
        notices: List of enriched NoticeData objects.
        filename: Optional filename override.
        keep_government: If True, include government-entity records.

    Returns:
        Path to the written CSV file.
    """
    if filename is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"datasift_upload_{timestamp}.csv"

    output_path = OUTPUT_DIR / filename
    written = 0
    incomplete = 0
    govt_dropped = 0
    issue_counts: dict[str, int] = {}

    built_rows = []
    for notice in notices:
        if not keep_government and _is_government_owner(notice):
            govt_dropped += 1
            continue
        _check_city_zip(notice)
        built_rows.append(_build_row(notice))
    built_rows, _ = clean_datasift_rows(built_rows)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for row in built_rows:
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
            writer.writerow(row)
            written += 1

    if govt_dropped:
        logger.info("Dropped %d government-entity records (Travis County, City Of, etc.)", govt_dropped)

    logger.info("Wrote %d records to DataSift CSV: %s", written, output_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        written - incomplete, written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", written, written)
    return output_path


def write_datasift_split_csvs(
    notices: list[NoticeData],
    date_str: str | None = None,
    keep_government: bool = False,
) -> list[dict]:
    """Generate separate DM and Heir Map CSVs for two-upload Message Board flow.

    CSV 1 ("DMs"): All records. Deceased get DM breakdown as Notes, living get
    property details. Creates/updates all records in DataSift.

    CSV 2 ("Heirs"): Only deceased records with heir data. Notes = full heir map.
    DataSift merges by address, adding a second Message Board comment.

    Government-owned records are dropped by default — pass keep_government=True
    to include them.

    Args:
        notices: List of enriched NoticeData objects.
        date_str: Optional date string for filenames/list names (default: today).
        keep_government: If True, include government-entity records.

    Returns:
        List of dicts: [{"path": Path, "label": str, "list_name": str}, ...]
        Returns 1 item if no deceased-with-heirs, 2 items otherwise.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results = []

    # CSV 1: DMs — all records (government dropped by default)
    dm_path = OUTPUT_DIR / f"datasift_upload_DMs_{timestamp}.csv"
    dm_written = 0
    incomplete = 0
    govt_dropped = 0
    issue_counts: dict[str, int] = {}
    with open(dm_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for notice in notices:
            if not keep_government and _is_government_owner(notice):
                govt_dropped += 1
                continue
            _check_city_zip(notice)
            row = _build_row(notice, notes_override=_build_dm_notes(notice))
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
            writer.writerow(row)
            dm_written += 1

    if govt_dropped:
        logger.info("Dropped %d government-entity records (Travis County, City Of, etc.)", govt_dropped)

    logger.info("DMs CSV: %d records → %s", dm_written, dm_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        dm_written - incomplete, dm_written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", dm_written, dm_written)
    results.append({
        "path": dm_path,
        "label": "DMs",
        "list_name": f"SiftStack {date_str} - DMs",
    })

    # CSV 2: Heirs — only deceased with heir data (still drops government)
    deceased_with_heirs = [
        n for n in notices
        if n.owner_deceased == "yes" and n.heir_map_json
        and (keep_government or not _is_government_owner(n))
    ]

    if deceased_with_heirs:
        heir_path = OUTPUT_DIR / f"datasift_upload_Heirs_{timestamp}.csv"
        heir_written = 0
        with open(heir_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
            writer.writeheader()
            for notice in deceased_with_heirs:
                row = _build_row(notice, notes_override=_build_heir_notes(notice))
                writer.writerow(row)
                heir_written += 1

        logger.info("Heirs CSV: %d records → %s", heir_written, heir_path)
        results.append({
            "path": heir_path,
            "label": "Heirs",
            "list_name": f"SiftStack {date_str} - Heirs",
        })
    else:
        logger.info("No deceased records with heir data — skipping Heirs CSV")

    return results


def write_datasift_by_notice_type(
    notices: list[NoticeData],
    date_str: str | None = None,
    keep_government: bool = False,
    split_by_county: bool = False,
) -> list[dict]:
    """One CSV per distress type (notice_type), plus a combined Heirs CSV.

    DataSift's upload wizard assigns every record in a CSV to a single list
    (the list name entered in Step 1 of the wizard). The per-record `Lists`
    column often stays unmapped during Step 4, so to land records in the
    right DataSift list we must upload one CSV per distress type.

    Output layout:
      - One CSV per notice_type present in `notices`, named `datasift_{type}_
        {timestamp}.csv`, with list name from NOTICE_TYPE_TO_LIST
        ("Foreclosure", "Probate", "Tax Sale", ...).
      - One final `datasift_heirs_{timestamp}.csv` (list name "Heirs")
        containing only deceased-with-heirs records with the full heir-map
        Notes — uploaded last so DataSift merges a second Message Board
        comment onto the records created by the earlier per-type uploads.
        Skipped if no deceased records have heir data.

    Government-entity owners (Travis County Trustee, City Of Lakeway, etc.)
    are dropped by default. Pass keep_government=True to include them.

    Args:
        notices: List of enriched NoticeData objects.
        date_str: Unused for list names (bare list names per OCTOLIST), kept
            for call-site parity with write_datasift_split_csvs(). Default: today.
        keep_government: If True, include government-entity records.

    Returns:
        List of dicts: [{"path": Path, "label": str, "list_name": str,
        "count": int}, ...]. One entry per notice_type present plus optional
        Heirs entry. Returns empty list if no records survive filtering.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results: list[dict] = []

    # Filter government-entity owners once up front.
    govt_dropped = 0
    filtered: list[NoticeData] = []
    for n in notices:
        if not keep_government and _is_government_owner(n):
            govt_dropped += 1
            continue
        filtered.append(n)

    if govt_dropped:
        logger.info("Dropped %d government-entity records (Travis County, City Of, etc.)", govt_dropped)

    # Group by notice_type (and county when split_by_county). Preserve the
    # canonical order from NOTICE_TYPE_TO_LIST so the uploader processes them in
    # a predictable sequence; unknown notice_type values sort last. When
    # splitting by county, the list name stays the notice type (so DataSift CRM
    # lists remain per-type and all counties merge into one list), but each
    # county gets its own CSV + a "county" field for per-county tracking.
    groups: dict[tuple[str, str], list[NoticeData]] = {}
    for n in filtered:
        ntype = (n.notice_type or "").strip().lower()
        county = (n.county or "").strip() if split_by_county else ""
        groups.setdefault((county, ntype), []).append(n)

    def _order_key(key: tuple[str, str]) -> tuple[int, str]:
        county, ntype = key
        try:
            type_rank = list(NOTICE_TYPE_TO_LIST).index(ntype)
        except ValueError:
            type_rank = len(NOTICE_TYPE_TO_LIST)
        return (type_rank, county)

    for county, ntype in sorted(groups, key=_order_key):
        group = groups[(county, ntype)]
        if not group:
            continue

        list_name = NOTICE_TYPE_TO_LIST.get(ntype)
        if not list_name:
            # Unknown notice_type — derive a sensible list name from the value.
            logger.warning("Unknown notice_type '%s' — deriving list name", ntype)
            list_name = ntype.replace("_", " ").title() if ntype else "Unknown"

        # Filename-safe slugs: lowercase with underscores. County prefix is
        # added to the filename only when splitting by county.
        slug = re.sub(r"[^a-z0-9]+", "_", ntype or "unknown").strip("_") or "unknown"
        county_slug = re.sub(r"[^a-z0-9]+", "_", county.lower()).strip("_")
        prefix = f"{county_slug}_" if county_slug else ""
        csv_path = OUTPUT_DIR / f"datasift_{prefix}{slug}_{timestamp}.csv"

        label_disp = f"{county} {list_name}".strip() if county else list_name
        # Build all rows, then run the final audit/cleaner before writing so the
        # delivered list is deduped + clean regardless of upstream messiness.
        built_rows = []
        for notice in group:
            _check_city_zip(notice)
            built_rows.append(_build_row(notice, notes_override=_build_dm_notes(notice)))
        built_rows, _ = clean_datasift_rows(built_rows, label=label_disp)

        written = 0
        incomplete = 0
        issue_counts: dict[str, int] = {}
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
            writer.writeheader()
            for row in built_rows:
                is_complete, issues = _validate_row(row)
                if not is_complete:
                    incomplete += 1
                    for issue in issues:
                        issue_counts[issue] = issue_counts.get(issue, 0) + 1
                writer.writerow(row)
                written += 1
        logger.info("%s CSV: %d records → %s", label_disp, written, csv_path)
        if incomplete:
            logger.warning(
                "  completeness: %d/%d clean, %d incomplete (%s)",
                written - incomplete, written, incomplete,
                ", ".join(f"{k}={v}" for k, v in issue_counts.items()),
            )

        results.append({
            "path": csv_path,
            "label": list_name,
            "list_name": list_name,
            "count": written,
            "county": county,
        })

    # Final Heirs CSV — deceased-with-heirs across all notice_types, uploaded
    # last so DataSift layers the heir-map Message Board comment onto records
    # created by the per-type uploads above.
    deceased_with_heirs = [
        n for n in filtered
        if n.owner_deceased == "yes" and n.heir_map_json
    ]

    if deceased_with_heirs:
        heir_path = OUTPUT_DIR / f"datasift_heirs_{timestamp}.csv"
        heir_written = 0
        with open(heir_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
            writer.writeheader()
            for notice in deceased_with_heirs:
                row = _build_row(notice, notes_override=_build_heir_notes(notice))
                writer.writerow(row)
                heir_written += 1

        logger.info("Heirs CSV: %d records → %s", heir_written, heir_path)
        results.append({
            "path": heir_path,
            "label": "Heirs",
            "list_name": "Heirs",
            "count": heir_written,
            "county": "",
        })
    else:
        logger.info("No deceased records with heir data — skipping Heirs CSV")

    return results


def write_cleanup_csv(cleanup_notices: list[NoticeData]) -> dict | None:
    """Write all drop-off cleanup rows to ONE CSV (record_status in sold/resolved).

    These are tag-update rows matched to an existing DataSift record by property
    address — "Sold" (a parcel that fell off the tax-delinquent roll) and
    "Code Violation Resolved" (a code-enforcement case that closed/complied).
    They are kept OUT of the per-type lead CSVs so they aren't auto-uploaded as
    new leads; this file is a review / manual-upload artifact that lands in the
    Kessair 03_Sold-Cleanup Drive folder.

    Returns {"path", "label", "count", "sold", "resolved"} or None if there are
    no cleanup records.
    """
    cleanup = [
        n for n in cleanup_notices
        if getattr(n, "record_status", "") in ("sold", "resolved")
    ]
    if not cleanup:
        return None

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"datasift_cleanup_{timestamp}.csv"
    sold = resolved = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for notice in cleanup:
            writer.writerow(_build_row(notice))
            if notice.record_status == "sold":
                sold += 1
            else:
                resolved += 1

    logger.info(
        "Cleanup CSV: %d records (%d Sold, %d Resolved) → %s",
        len(cleanup), sold, resolved, csv_path,
    )
    return {
        "path": csv_path,
        "label": "Cleanup",
        "count": len(cleanup),
        "sold": sold,
        "resolved": resolved,
    }

"""Cleaning engine for Travis County tax-delinquent rows.

Ported from the `travis-county-texdel-clean` Claude skill. Pure stdlib,
no I/O — operates on already-parsed CSV dict rows. The skill's Excel
and openpyxl logic is intentionally dropped; SiftStack is CSV-native.

Public surface used by `tax_delinquent_travis.py`:
    is_name_overflow, is_business, title_case
    clean_mailing_address, validate_address
    build_zip_lookup, resolve_blank_zip
    STREET_SUFFIXES, HOA_PAT, TARGET_ZIPS
"""

from __future__ import annotations

import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────
# Travis-specific target ZIPs. Kept in sync with the "Travis" array in
# src/target_zips.json (manual override 2026-06-29) so Travis tax-delinquent
# filters identically to every other Travis source. Other counties have
# their own filter list in src/target_zips.json.
TARGET_ZIPS: set[str] = {
    "78745", "78660", "78723", "78746", "78704", "78757", "78759", "78731",
    "78749", "78727", "78748", "78738", "78753", "78702", "78664", "78739",
    "78758", "78728", "78641", "78733", "78732", "78645", "78705", "78747",
    "78754", "78734", "78724", "78751",
    # added 2026-07-24 — active Travis gaps vs the DataSift filter
    "78744", "78653", "78725", "78701",
    # restored 2026-08-06 — core east/central Austin ZIPs that the 2026-06-29
    # market refresh dropped. They are on the original county-data target list
    # and still carry real delinquent volume (78721 n=224, 78741 n=214,
    # 78752 n=90, 78703 n=57, 78735 n=54 on the 2026-08-06 roll).
    "78703", "78721", "78735", "78741", "78752",
}

# Condo property-type code filtered out per skill spec
EXCLUDED_PROP_TYPES: set[str] = {"A4"}


# ── Property ZIP → city ───────────────────────────────────────────────
# The delinquent roll has NO property-city column — only PROPZIP. The scraper
# used to seed Property City from the owner's MAILING city, which is right for
# owner-occupants and badly wrong for absentees: "5505 Manor Rd / Wilmington /
# TX 78723" for an Austin property, plus Houston/San Angelo/Dallas scattered
# through a Travis-only list. Smarty can't rescue it either — it runs
# MatchType.STRICT and simply returns no candidate when the city contradicts
# the ZIP, so the bad value passes straight through to DataSift.
#
# Derived from the roll itself: for each PROPZIP, the modal mailing city among
# OWNER-OCCUPANT rows (mailing street contains the property street), where the
# mailing city *is* the property city by definition. min n=3 and >=50% purity;
# most are 95-100%. That derivation now lives in build_zip_city_lookup() below
# and runs on every scrape, so the table can be re-verified (and unknown ZIPs
# resolved) without a separate script — it reproduces these 58 entries exactly
# from both a post-flip and a pre-flip roll, verified 2026-08-06.
#
# Multi-city ZIPs (78734 Lakeway, 78738 Bee Cave, 78746 West Lake Hills) map to
# the USPS default city name, which is the deliverable one for direct mail.
ZIP_CITY: dict[str, str] = {
    "78610": "Buda",            "78612": "Cedar Creek",
    "78613": "Cedar Park",      "78615": "Coupland",
    "78617": "Del Valle",       "78620": "Dripping Springs",
    "78621": "Elgin",           "78634": "Hutto",
    "78641": "Leander",         "78645": "Lago Vista",
    "78652": "Manchaca",        "78653": "Manor",
    "78654": "Marble Falls",    "78660": "Pflugerville",
    "78664": "Round Rock",      "78669": "Spicewood",
    "78701": "Austin", "78702": "Austin", "78703": "Austin",
    "78704": "Austin", "78705": "Austin", "78719": "Austin",
    "78721": "Austin", "78722": "Austin", "78723": "Austin",
    "78724": "Austin", "78725": "Austin", "78726": "Austin",
    "78727": "Austin", "78728": "Austin", "78729": "Austin",
    "78730": "Austin", "78731": "Austin", "78732": "Austin",
    "78733": "Austin", "78734": "Austin", "78735": "Austin",
    "78736": "Austin", "78737": "Austin", "78738": "Austin",
    "78739": "Austin", "78741": "Austin", "78742": "Austin",
    "78744": "Austin", "78745": "Austin", "78746": "Austin",
    "78747": "Austin", "78748": "Austin", "78749": "Austin",
    "78750": "Austin", "78751": "Austin", "78752": "Austin",
    "78753": "Austin", "78754": "Austin", "78756": "Austin",
    "78757": "Austin", "78758": "Austin", "78759": "Austin",
    # Boundary ZIPs (Williamson/Hays edge parcels + the UT campus ZIP). These
    # appear only once or twice per roll, so they can never reach
    # MIN_ZIP_CITY_VOTES and are not derivable — without them 78681/78628/
    # 78642/78619 would each fall back to "Austin", which is simply wrong.
    # Values are the USPS default city, added 2026-08-06.
    "78619": "Driftwood",       "78626": "Georgetown",
    "78628": "Georgetown",      "78633": "Georgetown",
    "78642": "Liberty Hill",    "78665": "Round Rock",
    "78681": "Round Rock",      "78712": "Austin",
    "78717": "Austin",
}


# Thresholds for accepting a ZIP→city pair derived from a roll at runtime.
# Same bar the static table above was built to: at least 3 owner-occupant
# votes and a simple majority for the winning city.
MIN_ZIP_CITY_VOTES = 3
MIN_ZIP_CITY_PURITY = 0.5

# Populated per run by learn_zip_cities(); consulted only for ZIPs the static
# table has never seen. Cleared and rebuilt on every call.
_LEARNED_ZIP_CITY: dict[str, str] = {}
# ZIPs already logged this run, so an unknown ZIP warns once, not 400 times.
_ZIP_CITY_LOGGED: set[str] = set()


def build_zip_city_lookup(rows) -> dict[str, str]:
    """Derive property-ZIP → property-city from the roll's own owner-occupant rows.

    The roll has no property-city column at all — only `Property Zip`. Its
    `City` column is the OWNER'S MAILING city, which equals the property city
    for an owner-occupant and is wrong for every absentee (Houston, Dallas,
    Miami Beach … on a Travis-only list).

    So: keep only rows whose mailing address contains the situs street name —
    those are owner-occupied, and their mailing city IS the property city —
    then take the modal city per ZIP. This is exactly how the static ZIP_CITY
    table was derived; running it per roll lets a ZIP the table has never seen
    resolve to a real city instead of silently defaulting to Austin.
    """
    votes: dict[str, Counter] = {}
    for row in rows:
        zip5 = (row.get("Property Zip") or "").strip()[:5]
        if len(zip5) != 5 or not zip5.isdigit():
            continue
        state = (row.get("State") or "").strip().upper()
        if state not in ("", "TX"):
            continue  # out-of-state mailing address is never the property city
        city = (row.get("City") or "").strip().upper()
        street = (row.get("Street Name") or "").strip().upper()
        if not city or len(street) < 4:
            continue
        mailing = " ".join(
            (row.get(k) or "").strip().upper()
            for k in ("Address 1", "Address 2", "Address 3")
        )
        if street not in mailing:
            continue  # absentee — mailing city is NOT the property city
        votes.setdefault(zip5, Counter())[city] += 1

    derived: dict[str, str] = {}
    for zip5, counter in votes.items():
        city, n = counter.most_common(1)[0]
        if n >= MIN_ZIP_CITY_VOTES and n / sum(counter.values()) >= MIN_ZIP_CITY_PURITY:
            derived[zip5] = title_case(city)
    return derived


def learn_zip_cities(rows) -> dict[str, str]:
    """Rebuild the per-run ZIP→city fallback from `rows`. Returns what it learned."""
    _LEARNED_ZIP_CITY.clear()
    _LEARNED_ZIP_CITY.update(build_zip_city_lookup(rows))
    _ZIP_CITY_LOGGED.clear()
    return dict(_LEARNED_ZIP_CITY)


def city_for_zip(zip5: str, default: str = "Austin") -> str:
    """Property city for a Travis property ZIP. NEVER use the mailing city.

    Static table first, then anything `learn_zip_cities()` derived from the
    current roll, then `default`. Falling through to `default` is logged once
    per ZIP: a silent "Austin" on a Del Valle or Pflugerville property is the
    exact failure this lookup exists to prevent.
    """
    key = (zip5 or "").strip()[:5]
    city = ZIP_CITY.get(key)
    if city:
        return city

    city = _LEARNED_ZIP_CITY.get(key)
    if city:
        if key not in _ZIP_CITY_LOGGED:
            _ZIP_CITY_LOGGED.add(key)
            logger.info(
                "ZIP %s is not in ZIP_CITY; derived %r from this roll's "
                "owner-occupant rows. Add it to travis_texdel_cleaner.ZIP_CITY.",
                key, city,
            )
        return city

    # "00000" is the roll's sentinel for "no property ZIP", not an unknown one —
    # resolve_blank_zip already handles those, so don't warn about them.
    if key and key != "00000" and key not in _ZIP_CITY_LOGGED:
        _ZIP_CITY_LOGGED.add(key)
        logger.warning(
            "ZIP %r has no property-city mapping and could not be derived from "
            "the roll — falling back to %r, which may be wrong.", key, default,
        )
    return default


# ── Street suffix dictionary ──────────────────────────────────────────
STREET_SUFFIXES: set[str] = {
    "ST", "AVE", "BLVD", "DR", "RD", "LN", "CT", "WAY", "HWY", "PKWY",
    "CIR", "PL", "LOOP", "TRL", "TER", "CV", "COVE", "PATH", "PASS",
    "SPUR", "XING", "RUN", "HILL", "GLN", "VLG", "EXPY", "FWY",
    "HIGHWAY", "CREEK", "MEADOW", "RIDGE", "VIEW", "SPRINGS", "PARK",
    "OAKS", "POINT", "TRACE", "CROSSING", "GROVE", "BEND", "LANDING",
    "HOLLOW", "KNOLL", "CANYON", "VALLEY", "CREST", "FIELD", "ROW",
    "GREEN", "CLIFF", "HARBOR", "SHORE", "LAKE", "FALLS", "BRIDGE",
    "WALK", "ALLEY", "PLAZA", "SQ", "SQUARE", "PLACE", "ROAD",
    "MOPAC", "LAMAR",
}


# ── Regex patterns ─────────────────────────────────────────────────────
PO_BOX_PAT = re.compile(r"P\.?\s*O\.?\s*BOX\s+\d+", re.I)
STREET_PAT = re.compile(r"^\d+\s+\w")
PO_PAT = re.compile(r"^P\.?O\.?\s*BOX", re.I)
CO_PAT = re.compile(r"^(C/?O\s|ATTN|%|@)", re.I)

BIZ_KW = re.compile(
    r"\b(LLC|LP|INC|CORP|LTD|TRUST|TRUSTEE|PARTNERSHIP|COMPANY|ENTERPRISES|"
    r"PROPERTIES|INVESTMENTS|REALTY|HOLDINGS|MGMT|MANAGEMENT|ASSOCIATES|"
    r"GROUP|SERVICES|CAPITAL|EQUITY|FUND|VENTURES|DEVELOPMENT|BUILDERS|"
    r"CONSTRUCTION|ASSOCIATION|FOUNDATION|MINISTRY|CHURCH|CITY\sOF|"
    r"STATE\sOF|COUNTY|DISTRICT|MUNICIPAL|BANK|CREDIT\sUNION)\b",
    re.I,
)

# `HOA` is an association marker only where an entity name puts it: as the
# first or last token, or immediately before a corporate suffix. Mid-name it is
# far more often the Vietnamese given name Hoa — Travis carries several
# ("HUYNH ANH HOA THI", "NGUYEN HOA V & OANH K", "HUYNH VAN NGO & THI HOA HO")
# and a bare \bHOA\b silently dropped every one of them as a homeowners
# association. Real HOAs in the roll are uniformly "<PLACE> HOA [INC]".
_HOA_ABBREV = r"(?:^\s*HOA\b|\bHOA\s*$|\bHOA\s+(?:INC|LLC|LTD|CORP|ASSOC\w*|ASSN)\b)"

BIZ_EXTRA = re.compile(
    r"\b(HOMEOWNERS|MUD\s*NO|UTILITY|REIT|SERIES|REVOCABLE|"
    r"IRREVOCABLE|LIVING\sTRUST|FAMILY\sTRUST|GST|EXEMPT)\b"
    rf"|{_HOA_ABBREV}",
    re.I,
)

HOA_PAT = re.compile(
    r"\b(HOMEOWNERS|HOME\s+OWNERS|OWNERS\s+ASSOC|PROPERTY\s+OWNERS)\b"
    rf"|{_HOA_ABBREV}",
    re.I,
)

# An estate is a probate lead — the highest-value kind — so it outranks any
# association marker. "ESTATE OF CHANG HENRY HOA" is a person, not an HOA.
ESTATE_PAT = re.compile(r"\b(ESTATE|EST)\s+OF\b|\bESTATE\s*$", re.I)


def is_hoa(name: str) -> bool:
    """True when the owner is a homeowners/property-owners association.

    Estate names win — see ESTATE_PAT.
    """
    if not name or ESTATE_PAT.search(name):
        return False
    return bool(HOA_PAT.search(name))


# ── Subdivision patterns used by blank-zip resolver ───────────────────
SUBDIV_PATTERNS: list[str] = [
    r"\b(NORTHRIDGE ACRES)", r"\b(LAGO VISTA ESTATES SEC \d+)",
    r"\b(BAR-K RANCHES PLAT \d+)", r"\b(NORTH SHORE COLONY)",
    r"\b(JONESTOWN HILLS UNIT \d+)", r"\b(PECAN TERRACE)",
    r"\b(SOUTH CHERRY HOLLOW)", r"\b(BUENA VISTA)",
    r"\b(GILBERT LANE SUBD)", r"\b(IMPERIAL VALLEY)",
    r"\b(SHADY LAKE ACRES)", r"\b(THORNBURY)",
    r"\b(LAMPLIGHT VILLAGE)", r"\b(TALLGRASS)",
    r"\b(WEBBERVILLE)", r"\b(WOODLAND OAKS)",
    r"\b(SWISS ALPINE)", r"\b(LYNN CONNIE)",
    r"\b(LEANDER HILLS)", r"\b(MEADOW LAKE)",
]


# ── Address cleaning engine ────────────────────────────────────────────
def find_address_start(text: str) -> int:
    """Return the index where the real street address starts in `text`.

    Scans for a digit sequence followed within 8 words by a known street
    suffix, skipping digit sequences that are immediately followed by
    another digit (not a street number). Also detects PO BOX patterns.
    Returns -1 when no address start is found.
    """
    po = PO_BOX_PAT.search(text)
    if po:
        return po.start()
    for m in re.finditer(r"(?<!\S)(\d+[A-Z]?)\s", text):
        candidate_pos = m.start(1)
        words = text[candidate_pos:].split()
        if len(words) < 2:
            continue
        if re.match(r"^\d+[A-Z]?$", words[1]):
            continue
        for w in words[:8]:
            if re.sub(r"[^A-Z]", "", w.upper()) in STREET_SUFFIXES:
                return candidate_pos
    return -1


def stage1_extract(raw: str) -> tuple[str, bool]:
    """Strip leading %/C-O/ATTN/FBO/CUSTODIAN-FBO/DBA contamination.

    Returns (cleaned, did_strip). If no contamination found, returns
    (raw, False).
    """
    if not raw:
        return "", False
    raw = str(raw).strip()
    if not raw:
        return "", False
    if re.match(r"^9{5,}$", raw):
        return "", True
    if raw.startswith("%") and len(raw) > 1 and raw[1] != " ":
        raw = "% " + raw[1:]
    prefix_match = re.match(
        r"^(%\s+|C/?O\s+|ATTN[:\s]+|FOR\s+|FBO\s+|CUSTODIAN\s+FBO\s+|D/?B/?A:?\s+|@\s+|ATT:\s+)",
        raw, re.I,
    )
    if prefix_match:
        remainder = raw[prefix_match.end():]
        pos = find_address_start(remainder)
        if pos >= 0:
            return remainder[pos:].strip(), True
        return remainder.strip(), True
    attn_match = re.search(r"\bATTN[:\s]+", raw, re.I)
    if attn_match:
        remainder = raw[attn_match.end():]
        pos = find_address_start(remainder)
        if pos >= 0:
            return remainder[pos:].strip(), True
    return raw, False


def stage2_postclean(addr: str) -> str:
    """Strip trailing contamination after a real address."""
    if not addr:
        return addr
    addr = re.sub(r"\s+%\s+.*$", "", addr)
    addr = re.sub(r"\s+C/?O\s+(?!RD\b|HWY\b|HIGHWAY\b).*$", "", addr, flags=re.I)
    addr = re.sub(r"\s+ATTN[:\s]+.*$", "", addr, flags=re.I)
    addr = re.sub(r"\s+FBO\s+.*$", "", addr, flags=re.I)
    addr = re.sub(r"\s+\w+\s+CO$", "", addr, flags=re.I)
    addr = re.sub(r"^CO-OWNERS\s*", "", addr, flags=re.I)
    if addr and not re.search(r"\d", addr):
        return ""
    return addr.strip()


def validate_address(addr: str) -> bool:
    """Zero-tolerance validation — no %, C/O (except CO RD/HWY), ATTN, FBO, 9999."""
    if not addr:
        return True
    a = str(addr).strip()
    if not a:
        return True
    if re.search(r"(?<!\w)%", a):
        return False
    if re.search(r"\bC/?O\s+(?!RD\b|HWY\b)", a, re.I):
        return False
    if re.search(r"\bATTN\b", a, re.I):
        return False
    if re.search(r"\bFBO\b", a, re.I):
        return False
    if re.match(r"^9{5,}$", a):
        return False
    return True


def clean_mailing_address(addr1: str, addr2: str, addr3: str) -> str:
    """Concatenate addr1/2/3, strip leading+trailing contamination, return cleaned."""
    raw = " ".join(p for p in [addr1, addr2, addr3] if p)
    mail_addr, _ = stage1_extract(raw)
    return stage2_postclean(mail_addr)


# ── Name processing ────────────────────────────────────────────────────
def is_name_overflow(owner: str, addr1: str) -> bool:
    """Return True when Address 1 actually contains owner-name overflow.

    Heuristics:
    - Owner name ends with `&` → Address 1 is a co-owner continuation.
    - Address 1 contains business keywords (LLC, TRUST, INC, etc.).
    - Address 1 is all uppercase letters with no digits and no street suffix.
    Returns False if Address 1 starts with a street number, PO BOX, or C/O.
    """
    if not addr1:
        return False
    a = str(addr1).strip()
    o = str(owner).strip()
    if not a:
        return False
    if STREET_PAT.match(a) or PO_PAT.match(a) or CO_PAT.match(a):
        return False
    if o.rstrip(",").rstrip().endswith("&"):
        return True
    if BIZ_KW.search(a) or BIZ_EXTRA.search(a):
        return True
    if re.match(r"^[A-Z\s\.\-\,\&\%\#]+$", a) and not re.search(r"\d", a):
        if not set(a.split()).intersection(
            {"ST", "AVE", "BLVD", "DR", "RD", "LN", "CT", "WAY", "HWY",
             "PKWY", "CIR", "PL", "LOOP"}
        ):
            return True
    return False


def is_business(name: str) -> bool:
    """Return True when the owner name looks like a business/entity."""
    return bool(name and (BIZ_KW.search(name) or BIZ_EXTRA.search(name)))


def strip_etux_etal(name: str, *, strip_spousal_tail: bool = True) -> str:
    """Strip trailing ETUX/ETVIR/ETAL clauses + spousal `& <name>` tails.

    Mirrors Bell + Wilco behavior so Travis names also lose their tail
    markers for the marketing CSV. The pristine raw name is preserved
    separately on `notice.tax_owner_name` for deep prospecting / Notes.

    Examples:
      "AUSTIN MUSEUM OF ART INC ETAL"   → "AUSTIN MUSEUM OF ART INC"
      "ROSENDO GOMEZ JR ETAL"           → "ROSENDO GOMEZ JR"
      "SMITH JOHN ETUX MARY"            → "SMITH JOHN"
      "DOE JANE & JOHN K"               → "DOE JANE"

    Pass `strip_spousal_tail=False` for names already classified as entities.
    The `&` branch cuts at the FIRST ` & `, which decapitates an entity whose
    own name contains one — "U & US HOMES LLC" → "U", and since the `LLC` went
    with it, `is_business()` then reads the remainder as a person and ships a
    fabricated one-letter owner. Entities have no spouse, so callers must
    classify on the PRISTINE name and disable this branch for businesses.
    """
    if not name:
        return ""
    tail = r"(?:(?:ETUX|ETVIR|ETAL)\b|&\s)" if strip_spousal_tail else r"(?:ETUX|ETVIR|ETAL)\b"
    return re.sub(
        rf"\s+{tail}.*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()


# Suffixes that Python's .title() / .capitalize() mangles — these stay
# upper-cased after `title_case()` post-processing. Includes legal-entity
# suffixes, Roman numerals (II–X), professional credentials.
_KEEP_UPPER_SUFFIXES = frozenset({
    "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
    "JR", "SR",
    "LLC", "LP", "LLP", "INC", "LTD", "CORP", "PLLC", "PA", "PC", "CO",
    "MD", "DDS", "DVM", "CPA", "ESQ",
    "USA", "TX", "HOA", "HUD", "VA", "FBO",
    "ETUX", "ETVIR", "ETAL",  # only seen if strip_etux_etal didn't run
})


def title_case(s: str) -> str:
    """Title-case a name, preserving Mc/O'/Roman numerals and biz suffixes."""
    if not s:
        return ""
    result = []
    for w in s.split():
        # Strip trailing punctuation for the suffix-check, but re-attach later.
        trail = ""
        core = w
        while core and core[-1] in ".,;":
            trail = core[-1] + trail
            core = core[:-1]
        wu = core.upper()
        if wu in _KEEP_UPPER_SUFFIXES:
            result.append(wu + trail)
        elif wu.startswith("MC") and len(wu) > 2:
            result.append("Mc" + wu[2:].capitalize() + trail)
        elif wu.startswith("O'") and len(wu) > 2:
            result.append("O'" + wu[2:].capitalize() + trail)
        else:
            result.append(core.capitalize() + trail)
    return " ".join(result)


# ── Blank-zip resolver ────────────────────────────────────────────────
def build_zip_lookup(rows: list[dict]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build (subdiv_zips, street_zips) lookups from rows that already have zips.

    `rows` is a list of CSV dict rows with at least 'Street Name',
    'Property Zip', 'Legal Description' keys.
    """
    subdiv_zips: dict[str, list[str]] = {}
    street_zips: dict[str, list[str]] = {}
    for r in rows:
        pzip = str(r.get("Property Zip") or "").strip()[:5]
        if not pzip or pzip == "00000":
            continue
        legal = str(r.get("Legal Description") or "").strip().upper()
        st_name = str(r.get("Street Name") or "").strip().upper()
        for pat in SUBDIV_PATTERNS:
            m = re.search(pat, legal, re.I)
            if m:
                key = m.group(1).strip().upper()
                subdiv_zips.setdefault(key, []).append(pzip)
        if st_name:
            street_zips.setdefault(st_name, []).append(pzip)
    return subdiv_zips, street_zips


def resolve_blank_zip(
    legal_desc: str,
    st_name_raw: str,
    subdiv_zips: dict[str, list[str]],
    street_zips: dict[str, list[str]],
) -> str:
    """Look up the most common zip for a row with blank Property Zip.

    Tries subdivision match first, then falls back to street-name match.
    Returns '' when neither yields a hit.
    """
    lg = str(legal_desc or "").upper()
    sn = str(st_name_raw or "").strip().upper()
    for pat in SUBDIV_PATTERNS:
        m = re.search(pat, lg, re.I)
        if m:
            key = m.group(1).strip().upper()
            if key in subdiv_zips:
                return Counter(subdiv_zips[key]).most_common(1)[0][0]
    if sn and sn in street_zips:
        return Counter(street_zips[sn]).most_common(1)[0][0]
    return ""


# ── Zip formatting ────────────────────────────────────────────────────
def fmt_zip(z: str) -> str:
    """Hyphenate 9-digit zips (78704-1234); pass through shorter values."""
    if not z:
        return ""
    z = str(z).strip()
    if len(z) > 5 and z.isdigit():
        return z[:5] + "-" + z[5:]
    return z

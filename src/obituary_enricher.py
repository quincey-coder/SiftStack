"""Enrich notices with obituary-confirmed deceased owner data.

Searches for obituaries matching each property owner, parses them with
Claude Haiku to confirm identity and extract survivors, then identifies
the decision-maker (heir/executor) for each deceased owner.

This catches deceased owners the county tax API hasn't flagged yet.
"""

import json
import logging
import os
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from datetime import datetime

import llm_client
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

MAX_TOKENS = 1024
SEARCH_DELAY_MIN = 0.5
SEARCH_DELAY_MAX = 1.0
PARALLEL_WORKERS = 6  # Concurrent heir verifications
FETCH_TIMEOUT = 20
MAX_OBITUARY_TEXT = 6000
MAX_ADDRESS_TEXT = 15000  # Larger limit for people search pages (CBC has 250+ results)

# Heir-verification LLM budget per deceased record. Each survivor we verify is a
# Claude call (obituary search + parse), and deceased heirs recurse into their
# own sub-heirs — so a single obituary listing a large family fans out into
# dozens of calls. On 2026-07-10/11 a couple of records with 20+ named survivors
# drove daily runs to 525-920 Claude calls (normal is ~30) and blew the Actor
# timeout mid-verification. These caps bound the verification budget per record:
# the closest heirs (spouse/children/executor) are verified first, and every
# survivor BEYOND the cap is still recorded in heir_map_json (status
# "unverified", flagged verification_skipped) so its name/relationship/city
# still reaches the DataSift Notes field — nothing is lost, we just don't spend
# an LLM call chasing peripheral relatives. Env-overridable.
MAX_HEIRS_VERIFIED = int(os.getenv("OBITUARY_MAX_HEIRS_VERIFIED", "6"))      # depth-0 survivors
MAX_SUBHEIRS_VERIFIED = int(os.getenv("OBITUARY_MAX_SUBHEIRS_VERIFIED", "6"))  # depth-1 sub-heirs
# Hard ceiling on recursion depth regardless of the max_heir_depth input, so a
# stray input can't reintroduce the deep heirs-of-heirs fan-out. 1 = verify
# survivors and, for any confirmed-deceased survivor, their direct sub-heirs.
MAX_HEIR_DEPTH_CEILING = int(os.getenv("OBITUARY_MAX_HEIR_DEPTH", "1"))

# Maximum years between DOD and notice filing date to accept an obituary match.
# Probate is typically filed within 1-2 years of death. 3 years gives margin.
MAX_DOD_GAP_YEARS = 3


def _dod_sanity_check(dod_str: str, notice: "NoticeData") -> bool:
    """Reject obituary matches where DOD is implausibly far from the notice date.

    Returns True if the DOD is plausible, False if it should be rejected.
    """
    if not dod_str or not dod_str.strip():
        return True  # No DOD to check — let other signals decide

    reference_date = notice.date_added or ""
    if not reference_date:
        return True  # No filing date to compare against

    try:
        dod = datetime.strptime(dod_str.strip(), "%Y-%m-%d")
        ref = datetime.strptime(reference_date.strip(), "%Y-%m-%d")
        gap_days = (ref - dod).days
        gap_years = gap_days / 365.25
        if gap_years > MAX_DOD_GAP_YEARS:
            logger.warning(
                "  DOD sanity check FAILED: DOD %s is %.1f years before filing date %s "
                "(max %d years) — likely wrong person",
                dod_str, gap_years, reference_date, MAX_DOD_GAP_YEARS,
            )
            return False
        if gap_days < -7:
            # DOD is (materially) after the record date — impossible/parse artifact
            # (e.g. the LLM lifted the obituary's publication or "today" date). A
            # 7-day window absorbs minor date skew.
            logger.warning(
                "  DOD sanity check FAILED: DOD %s is after record date %s — invalid match",
                dod_str, reference_date,
            )
            return False
    except ValueError:
        return True  # Unparseable date — don't block on format issues

    return True


# Domains that always return HTTP 403 — skip direct fetch, go straight to Firecrawl
KNOWN_403_DOMAINS = {
    "dignitymemorial.com",
    "tributearchive.com",
    "echovita.com",
    "funeralhomes.com",
}

# Known obituary domains — filter search results to these
OBITUARY_DOMAINS = {
    "legacy.com",
    "dignitymemorial.com",
    "echovita.com",
    "tributearchive.com",
    "findagrave.com",
    "obituaries.com",
    "funeralhomes.com",
    "meaningfulfunerals.net",
    "mem.com",
    "forevermissed.com",
    "statesman.com",
}

# Legal suffixes to strip from tax API owner names before searching
_SUFFIX_RE = re.compile(
    r"\s*\(?\s*(?:LIFE\s+EST(?:ATE)?|PERSONAL\s+REP(?:RESENTATIVE)?|"
    r"TRUSTEE|TRUST|ETAL|ET\s+AL)\s*\)?\s*$",
    re.IGNORECASE,
)

# Suffixes that appear in notice owner_name but not tax API names
_NOTICE_SUFFIX_RE = re.compile(
    r",?\s*(?:Tenants?\s+By\s+The\s+Entirety|As\s+Joint\s+Tenants?|Joint\s+Tenants?)"
    r"|,?\s*(?:An?\s+)?(?:Unmarried|Married|Single)\s*(?:Person|Woman|Man)?"
    r"|,?\s*(?:Husband\s+And\s+Wife|Wife\s+And\s+Husband)"
    r"|\s+(?:Et\s+Al|Etal)\b",
    re.IGNORECASE,
)

# Business entity / trust / estate patterns — imported from shared config
import config as _cfg
_BUSINESS_RE = _cfg.BUSINESS_RE
_TRUST_NAME_RE = _cfg.TRUST_NAME_RE
_ESTATE_OF_RE = _cfg.ESTATE_OF_RE

# Common nickname ↔ formal name pairs for search fallback
_NICKNAME_MAP: dict[str, list[str]] = {
    "robert": ["bob", "bobby", "rob", "robbie"],
    "william": ["bill", "billy", "will", "willie", "willy"],
    "richard": ["rick", "dick", "rich"],
    "james": ["jim", "jimmy", "jamie"],
    "john": ["jack", "johnny"],
    "charles": ["chuck", "charlie"],
    "thomas": ["tom", "tommy"],
    "joseph": ["joe", "joey"],
    "michael": ["mike", "mikey"],
    "david": ["dave", "davey"],
    "daniel": ["dan", "danny"],
    "edward": ["ed", "eddie", "ted", "teddy"],
    "kenneth": ["ken", "kenny"],
    "ronald": ["ron", "ronnie"],
    "donald": ["don", "donnie"],
    "steven": ["steve", "stephen"],
    "stephen": ["steve", "steven"],
    "patrick": ["pat", "patty"],
    "raymond": ["ray"],
    "timothy": ["tim", "timmy"],
    "anthony": ["tony"],
    "lawrence": ["larry"],
    "gerald": ["jerry"],
    "eugene": ["gene"],
    "margaret": ["maggie", "peggy", "marge"],
    "elizabeth": ["liz", "beth", "betty", "eliza"],
    "patricia": ["pat", "patty", "trish"],
    "barbara": ["barb", "barbie"],
    "catherine": ["cathy", "kate", "kathy"],
    "katherine": ["kathy", "kate", "cathy"],
    "dorothy": ["dot", "dotty"],
    "virginia": ["ginny"],
    "deborah": ["debbie", "deb"],
    "sandra": ["sandy"],
}

# Build reverse map: nickname → [formal names]
_NICKNAME_REVERSE: dict[str, list[str]] = {}
for _formal, _nicks in _NICKNAME_MAP.items():
    for _nick in _nicks:
        _NICKNAME_REVERSE.setdefault(_nick, []).append(_formal)


def _get_name_variants(first_name: str) -> list[str]:
    """Return alternate first names (nicknames/formal) for search fallback."""
    key = first_name.lower().strip()
    variants = set()
    # Formal → nicknames
    if key in _NICKNAME_MAP:
        variants.update(_NICKNAME_MAP[key])
    # Nickname → formal names
    if key in _NICKNAME_REVERSE:
        variants.update(_NICKNAME_REVERSE[key])
    variants.discard(key)
    return list(variants)


# ── Survivor grounding guard ─────────────────────────────────────────
# A stronger model (see _obituary_model) plus a hardened prompt reduce the odds of
# hallucination; this guard is the deterministic backstop. We keep a survivor ONLY
# when their full name appears as an ADJACENT phrase in the source obituary text.
# Requiring adjacency (not just that each word appears somewhere) is what defeats
# confabulated heir maps: an invented spouse/child, OR a fake name recombined from
# scattered words ("Bowie Willis", "Emory Willis", a namesake "Norman Willis Jr").
#
# Intentional tradeoff: a survivor the obituary mentioned by FIRST NAME ONLY is not
# retained — we cannot ground a model-guessed surname. That is the safe direction:
# losing a low-value partial-name lead beats acting on a fabricated heir, and
# primary heirs (spouse/children) are almost always written in full and kept.

_GENERATIONAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _obituary_model() -> str:
    """High-quality model for heir-determining obituary parsing (configurable)."""
    return getattr(_cfg, "OBITUARY_LLM_MODEL", "claude-sonnet-4-6")


def _survivor_name(item) -> str:
    """Pull the name out of a survivor entry (dict with 'name', or a bare string)."""
    if isinstance(item, dict):
        return (item.get("name") or "").strip()
    if isinstance(item, str):
        return item.strip()
    return ""


def _ascii_fold(text: str) -> str:
    """Lowercase and strip accents so 'José' matches 'Jose' (prevents false drops)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _name_core_tokens(name: str) -> list[str]:
    """Accent-folded name tokens with generational suffixes (Jr/Sr/III) removed."""
    folded = re.sub(r"[^a-z ]", " ", _ascii_fold(name))
    return [t for t in folded.split() if t and t not in _GENERATIONAL_SUFFIXES]


def _name_is_grounded(name: str, source_norm: str,
                      deceased_first: str, deceased_surname: str) -> bool:
    """True only if the person's full name appears as an adjacent phrase in the text.

    - Single-token names: that token must appear as a whole word.
    - Multi-token names: the first and last name must appear adjacently, allowing
      up to two intervening tokens for middle names/initials (so "Elizabeth A.
      Holland" still matches the text "Elizabeth A. Holland"). Adjacency is what
      blocks fabricated names recombined from scattered words.
    - A "survivor" whose first+last equals the deceased's own name is dropped (it is
      the decedent, or a namesake duplicate, not a separate heir).

    Matching is literal against the accent-folded source text (no nickname
    expansion): the hardened prompt asks the model to return names verbatim.
    """
    core = _name_core_tokens(name)
    if not core:
        return False
    first, last = core[0], core[-1]
    if first == deceased_first and last == deceased_surname:
        return False  # the decedent themselves / a namesake, not an heir
    if len(core) == 1:
        return re.search(rf"\b{re.escape(first)}\b", source_norm) is not None
    # Require first .. last to occur as an adjacent span (<= 2 middle tokens).
    pattern = rf"\b{re.escape(first)}\b(?:\s+\w+){{0,2}}\s+{re.escape(last)}\b"
    return re.search(pattern, source_norm) is not None


def _validate_survivors_against_text(items, source_text, deceased_name=""):
    """Drop any survivor/heir whose full name is not grounded in the source text.

    Accepts a list of survivor dicts or plain name strings and returns the same
    shape, minus ungrounded entries. The deterministic backstop against
    confabulated heir maps. When there is no usable source text we do NOT filter
    (avoid false drops when grounding is impossible).
    """
    if not items or not source_text or not source_text.strip():
        return items
    source_norm = re.sub(r"[^a-z ]", " ", _ascii_fold(source_text))
    if not source_norm.strip():
        return items
    dcore = _name_core_tokens(deceased_name)
    deceased_first = dcore[0] if dcore else ""
    deceased_surname = dcore[-1] if dcore else ""

    kept, dropped = [], []
    for item in items:
        name = _survivor_name(item)
        if name and _name_is_grounded(name, source_norm, deceased_first, deceased_surname):
            kept.append(item)
        elif name:
            dropped.append(name)
    if dropped:
        logger.warning(
            "Grounding guard dropped %d ungrounded name(s) absent (as an adjacent "
            "phrase) from the source obituary: %s", len(dropped), "; ".join(dropped)[:400],
        )
    return kept


def _extract_personal_from_trust_estate(raw_name: str) -> str | None:
    """Extract a personal name from trust/estate ownership patterns.

    Examples:
        "JOHN DOE TRUST" → "JOHN DOE"
        "THE JOHN DOE REVOCABLE TRUST" → "JOHN DOE"
        "ESTATE OF MARY SMITH" → "MARY SMITH"
        "JOHN DOE LIVING TRUST" → "JOHN DOE"
        "FROST BANK TRUST" → None (business entity)
    """
    name = raw_name.strip()

    for pattern in (_TRUST_NAME_RE, _ESTATE_OF_RE):
        m = pattern.match(name)
        if m:
            extracted = m.group(1).strip()
            if _BUSINESS_RE.search(extracted):
                return None
            if len(extracted.split()) >= 2:
                return extracted

    return None


SYSTEM_PROMPT = (
    "You analyze obituaries to determine if a deceased person matches a property owner. "
    "Return ONLY valid JSON with no markdown formatting, no code fences, no explanation."
)

OBITUARY_PROMPT = """\
I have a property record with this owner information:
- Owner name: {owner_name}
- Property city: {city}
- Property state: Texas
- Property address: {address}

Below is text from a potential obituary. Determine if this obituary is for the same person \
as the property owner. Consider: name match (first + last name must match; middle name/initial \
is bonus confirmation), location match (same city or county in Texas), and timeline \
plausibility (death within last 5 years is typical for active foreclosure/tax sale records).

Return a JSON object with these exact keys:
- "match": true if this obituary is very likely for the property owner, false otherwise
- "confidence": "high", "medium", or "low"
- "full_name": the deceased person's full name from the obituary
- "date_of_death": date of death in YYYY-MM-DD format (empty string if not found)
- "city": city where they lived/died
- "state": two-letter US state where the deceased lived or died (e.g. "TX", "KS"); empty string if not stated
- "age_at_death": integer age at death (0 if not found)
- "survivors": array of objects with "name", "relationship", and "city" keys for each surviving family member. \
Extract ALL survivors mentioned anywhere in the text. Look for "survived by", "leaves behind", "cherished by", \
"loving family includes", and any named persons. Include: spouse/wife/husband, children (sons, daughters), \
stepchildren, grandchildren, siblings (brothers, sisters), parents, nieces, nephews, and in-laws. \
If a relationship is unclear from context, use "family_member" rather than omitting the person. \
Use each survivor's name exactly as written in the text. Do NOT append or guess a surname for \
someone the obituary names by first name only — return the name verbatim or omit it. \
(city is where the survivor lives if mentioned, empty string if not stated)
- "preceded_in_death": array of names of family members who predeceased them
- "executor_named": name of executor/personal representative if mentioned, empty string if not

Important: Only set "match" to true if the first AND last name match the owner. \
If the owner and obituary both have a middle name/initial and they CONFLICT \
(e.g. owner "Michael T" vs obituary "Michael Ralph"), set match=false — that is a \
different person. If the obituary is clearly anchored in a state other than Texas \
and the text shows no Texas connection, set match=false. Common names need location \
confirmation. Be conservative; a false negative is better than a false positive.

CRITICAL grounding rule: For "survivors", "preceded_in_death", and "executor_named", \
include ONLY people whose names are written verbatim in the obituary text below. Never \
infer, assume, or invent a spouse, child, or any other relative who is not literally \
named in the text. If the text names no survivors, return an empty array.

Obituary text:
{obituary_text}"""


_GEN_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "ESQ"}

# Alias / "also known as" noise tokens that should never count toward a
# name match (a court "a/k/a" can leak into a decedent name).
_NAME_ALIAS_NOISE = {"AKA", "FKA", "NKA", "DBA"}


def _gen_suffix(name: str) -> str:
    """Return the generational suffix token (JR/SR/II/III/...) found in a name,
    upper-cased, or '' if none. Scans all tokens so a suffix in any position is
    detected."""
    if not name:
        return ""
    cleaned = _SUFFIX_RE.sub("", name.upper())
    for tok in reversed(re.findall(r"[A-Z]+", cleaned)):
        if tok in _GEN_SUFFIXES:
            return tok
    return ""


def _person_tokens(name: str) -> set[str]:
    """Core (>1 char) alphabetic tokens of a name, upper-cased. Drops legal
    suffixes, generational suffixes (Jr/Sr/III), 'a/k/a' alias noise, and
    single-letter middle initials so the *core* identity compares cleanly.
    Generational suffixes are handled separately by _is_same_person."""
    if not name:
        return set()
    cleaned = _SUFFIX_RE.sub("", name.upper())
    toks = {t for t in re.findall(r"[A-Z]+", cleaned) if len(t) > 1}
    return toks - _GEN_SUFFIXES - _NAME_ALIAS_NOISE


def _is_same_person(a: str, b: str) -> bool:
    """True if two names denote the same person regardless of word order.

    Handles the CAD/probate case where the appraisal owner is the decedent
    themselves stored surname-first ('Cash Margot Suzanne') vs the court
    decedent name ('Margot Suzanne Cash'). Tolerant of a missing middle name:
    matches when one name's core token set is a subset of the other's.

    Generational suffixes then disambiguate same-core names: a 'Jr'/'II'/'III'
    is a different (younger) person from a 'Sr'/unmarked elder — so
    'Shelby Johnson Jr' (the child) is NOT the decedent 'Shelby Johnson'.
    'Sr' and no-suffix share the same 'elder' bucket.
    """
    ta, tb = _person_tokens(a), _person_tokens(b)
    if not ta or not tb:
        return False
    if not (ta <= tb or tb <= ta):
        return False

    def _bucket(s: str) -> str:
        return "" if s in ("", "SR") else s

    return _bucket(_gen_suffix(a)) == _bucket(_gen_suffix(b))


def _middle_name_conflict(owner_name: str, obit_name: str) -> bool:
    """True when owner and obituary names carry *conflicting* middle names/
    initials (e.g. 'Michael T Conner' vs 'Michael Ralph Conner' — a different
    person the LLM let through on first+last alone).

    Tolerant when either side omits a middle (records routinely do): the veto
    fires only when BOTH names have a middle token and none are compatible.
    Compatible = equal tokens, or an initial matching the other's first letter
    ('T' vs 'Thomas'). Word order is irrelevant, so a surname-first tax name
    ('CONNER MICHAEL T') compares correctly against 'Michael Ralph Conner'.
    """
    ow = [t for t in re.findall(r"[A-Z]+", (owner_name or "").upper()) if t not in _GEN_SUFFIXES]
    ob = [t for t in re.findall(r"[A-Z]+", (obit_name or "").upper()) if t not in _GEN_SUFFIXES]
    if len(ow) < 2 or len(ob) < 2:
        return False
    core = {ob[0], ob[-1]}          # obituary first + last name
    owner_mids = [t for t in ow if t not in core]
    obit_mids = [t for t in ob if t not in core]
    if not owner_mids or not obit_mids:
        return False

    def _compat(x: str, y: str) -> bool:
        if len(x) == 1 or len(y) == 1:
            return x[0] == y[0]
        return x == y

    for x in owner_mids:
        for y in obit_mids:
            if _compat(x, y):
                return False
    return True


_US_STATE_ABBR = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
}
_US_STATE_CODES = set(_US_STATE_ABBR.values())


def _norm_state(s: str) -> str:
    """Normalize a state string to a 2-letter USPS code, or '' if unrecognized."""
    s = (s or "").strip().upper()
    if not s:
        return ""
    if s in _US_STATE_CODES:
        return s
    return _US_STATE_ABBR.get(s, "")


def _validate_obituary_match(parsed: dict, notice, owner_name: str,
                             source_type: str, obituary_text: str = "") -> tuple[bool, str]:
    """Post-LLM sanity gate applied to every accepted obituary match.

    Catches wrong-person matches the LLM confirmed on weak evidence. Returns
    (accept, reject_reason). A False result should drop the match entirely —
    a false negative is cheaper than mailing the wrong heir.
    """
    if not parsed:
        return False, "no parse"
    obit_name = (parsed.get("full_name") or "").strip()

    # Conflicting middle names → almost always a same-first/last different person.
    if obit_name and _middle_name_conflict(owner_name, obit_name):
        return False, (f"middle-name conflict: owner '{owner_name}' vs "
                       f"obituary '{obit_name}'")

    # Geographic veto: every property here is in Texas. An obituary anchored in
    # another state with NO Texas tie is almost always a same-name different
    # person (e.g. an Austin owner matched to a Kansas obituary). We only veto
    # when the out-of-state signal is clear and there is no Texas connection —
    # a Texan can legitimately have an out-of-state funeral, so we stay lenient.
    obit_state = _norm_state(parsed.get("state", ""))
    if obit_state and obit_state != "TX":
        prop_city = (getattr(notice, "city", "") or "").strip().upper()
        obit_city = (parsed.get("city") or "").strip().upper()
        hay = f"{obituary_text}\n{parsed.get('city','')}".upper()
        tx_tie = (
            "TEXAS" in hay
            or bool(re.search(r"\bTX\b", hay))
            or (prop_city and prop_city == obit_city)
            or (prop_city and prop_city in hay)
        )
        if not tx_tie:
            return False, (f"geographic mismatch: obituary in {obit_state}, "
                           f"property in TX ({getattr(notice, 'city', '')})")

    return True, ""


def _surname(name: str) -> str:
    """Last significant (>1 char) token of a FIRST [MIDDLE] LAST name, upper-cased,
    with legal and generational suffixes (Jr/Sr/III/...) removed. '' when no
    usable token exists."""
    if not name:
        return ""
    cleaned = _SUFFIX_RE.sub("", name)
    toks = [t.upper() for t in re.findall(r"[A-Za-z]+", cleaned) if len(t) > 1]
    while toks and toks[-1] in _GEN_SUFFIXES:
        toks.pop()
    return toks[-1] if toks else ""


def _probate_dm_relationship(notice) -> tuple[str, str]:
    """Classify a probate-preset decision maker and explain the call.

    The DM here is the CAD owner-of-record, not a court-confirmed executor — the
    scraper never captures an executor. Use the appraisal deed to label honestly:

      • Joint deed ("SLAWSON JAMES D & VERNA") + survivor shares the decedent's
        surname  → "surviving spouse" (the obvious married-couple-on-title case)
      • Joint deed, different surname                → "surviving co-owner"
      • No joint marker (survivor already holds sole title) → "owner of record"

    Returns (relationship, dm_confidence_reason).
    """
    raw = f" {(notice.tax_owner_name or '').upper()} "
    has_etux = bool(re.search(r"\bET\s*(?:UX(?:OR)?|VIR)\b", raw))
    is_joint = "&" in raw or " AND " in raw or has_etux
    dm_surname = _surname(notice.owner_name)
    same_surname = bool(dm_surname) and dm_surname == _surname(notice.decedent_name)

    # Surviving child: the owner shares the decedent's surname but carries a
    # younger generational suffix (Jr/II/III...) the decedent lacks — i.e. the
    # son/daughter, not the spouse.
    dm_suffix = _gen_suffix(notice.owner_name)
    dec_suffix = _gen_suffix(notice.decedent_name)
    _YOUNGER = {"JR", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"}
    if same_surname and dm_suffix in _YOUNGER and dm_suffix != dec_suffix:
        return (
            "surviving child",
            f"surviving child — shares the decedent's surname with a {dm_suffix.title()} "
            "suffix (likely son/daughter)",
        )

    # ET UX / ET VIR explicitly names a spouse — strongest spouse signal.
    if has_etux and same_surname:
        return (
            "surviving spouse",
            "surviving spouse — named via ET UX/ET VIR on the appraisal deed",
        )
    if is_joint and same_surname:
        return (
            "surviving spouse",
            "surviving spouse — joint owner on the appraisal deed sharing the decedent's surname",
        )
    if is_joint:
        return (
            "surviving co-owner",
            "surviving co-owner on the appraisal deed",
        )
    return (
        "owner of record",
        "current owner of record on the appraisal deed (not a court-confirmed executor)",
    )


def parse_tax_owner_name(raw: str) -> list[str]:
    """Convert tax API owner name to search-friendly format(s).

    Tax API names are "LAST FIRST MIDDLE" with possible legal suffixes.
    Returns a list of names including middle initial when available,
    since common names need the initial for accurate obituary matching.

    Examples:
        "WILLIAMS DANIEL H" → ["Daniel H Williams"]
        "WILLIAMS DANIEL H & CHRISTINE C" → ["Daniel H Williams", "Christine Williams"]
        "BLALOCK GARY W % BLALOCK MISTY D" → ["Gary W Blalock"]
        "JONES ROBERT (LIFE EST)" → ["Robert Jones"]
        "EASTSIDE REAL ESTATE LLC" → [] (business entity)
    """
    if not raw or not raw.strip():
        return []

    name = raw.strip()

    # Extract personal name from trust/estate structures before business check
    # "ESTATE OF X" → FIRST LAST format, return directly
    # "X TRUST" → still LAST FIRST in tax records, continue parsing below
    if _ESTATE_OF_RE.match(name):
        personal = _extract_personal_from_trust_estate(name)
        if personal:
            return [personal.title()]
    personal = _extract_personal_from_trust_estate(name)
    if personal:
        name = personal

    # Skip business entities
    if _BUSINESS_RE.search(name):
        return []

    # Strip legal suffixes
    name = _SUFFIX_RE.sub("", name).strip()

    # Handle care-of (%) — only search the primary name (before %)
    if "%" in name:
        name = name.split("%")[0].strip()

    # Capture an ET UX[OR] / ET VIR spouse named after the primary owner. The
    # Latin marker (et uxor = "and wife", et vir = "and husband") introduces a
    # given name that shares the primary's surname
    # ("HAGLER JOHN B ETUX DIXIE" -> "Dixie Hagler"). Pull the clause out so the
    # primary parses cleanly; the spouse is appended as a co-owner after the
    # main parse below. (ETAL / ET AL is handled earlier by _SUFFIX_RE.)
    etux_given = ""
    _etux_m = re.search(r"\bET\s*(?:UX(?:OR)?|VIR)\b\s*(.*)$", name, flags=re.IGNORECASE)
    if _etux_m:
        etux_given = _etux_m.group(1).strip()
        name = name[:_etux_m.start()].strip()

    # Handle joint owners with &
    parts = re.split(r"\s*&\s*", name)

    results = []
    last_name = ""

    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue

        tokens = part.split()

        if i == 0:
            # First part: "LAST FIRST [MIDDLE] [SUFFIX]"
            if len(tokens) < 2:
                continue
            # Pull a trailing generational suffix (JR/SR/II/III...) off so it
            # lands at the END of the display name instead of being mistaken for
            # a middle initial — and so it survives for downstream Jr/Sr logic.
            gen_suffix = ""
            if tokens[-1].upper() in _GEN_SUFFIXES and len(tokens) > 2:
                gen_suffix = tokens[-1].title()
                tokens = tokens[:-1]
            last_name = tokens[0]
            first_name = tokens[1]
            middle = tokens[2] if len(tokens) >= 3 else ""
            if middle and len(middle) <= 2:
                built = f"{first_name.title()} {middle.title()} {last_name.title()}"
            else:
                built = f"{first_name.title()} {last_name.title()}"
            if gen_suffix:
                built = f"{built} {gen_suffix}"
            results.append(built)
        else:
            # A lone first name after "&" is a co-owner who inherits the primary
            # surname ("WANG JUNGANG & XIUQIN" -> "Xiuqin Wang"). Without this the
            # surviving joint owner is dropped entirely.
            if len(tokens) == 1:
                if last_name:
                    results.append(f"{tokens[0].title()} {last_name.title()}")
                continue
            # Co-owner segment that restates the family surname in FIRST [MIDDLE]
            # LAST order ("COBELLE COURTNEY & SUZANNE COBELLE" -> "Suzanne
            # Cobelle"). Detect by the trailing token matching the primary
            # surname; otherwise the LAST-FIRST branch below would flip it to
            # "Cobelle Suzanne".
            if last_name and tokens[-1].upper() == last_name.upper():
                first_name = tokens[0]
                results.append(f"{first_name.title()} {last_name.title()}")
                continue
            # Subsequent parts: "FIRST [MIDDLE]" or "LAST FIRST [MIDDLE]"
            # If second token is a single char (middle initial), it's "FIRST MIDDLE"
            # inheriting previous last name. Otherwise check for new last name.
            is_middle_initial = len(tokens) == 2 and len(tokens[1]) <= 2
            if not is_middle_initial and len(tokens) >= 2 and tokens[0].upper() != last_name.upper():
                # Different last name: "BLALOCK MISTY D" (3+ tokens, first differs)
                new_last = tokens[0]
                first_name = tokens[1]
                middle = tokens[2] if len(tokens) >= 3 else ""
                if middle and len(middle) <= 2:
                    results.append(f"{first_name.title()} {middle.title()} {new_last.title()}")
                else:
                    results.append(f"{first_name.title()} {new_last.title()}")
            else:
                # Same last name: "CHRISTINE C" (inherits WILLIAMS)
                first_name = tokens[0]
                results.append(f"{first_name.title()} {last_name.title()}")

    # Append the ET UX[OR]/ET VIR spouse, sharing the primary surname.
    if etux_given and last_name:
        spouse_first = etux_given.split()[0]
        spouse_full = f"{spouse_first.title()} {last_name.title()}"
        if spouse_full not in results:
            results.append(spouse_full)

    return results


def _estate_display_name(notice) -> str:
    """Best available FIRST-LAST person name for an 'Estate of …' fallback.

    code_violation (and other CAD-sourced-owner) records leave ``owner_name``
    blank by design — the deceased's real name lives in ``tax_owner_name``
    (raw LAST FIRST MIDDLE / NAMELF). Resolving only from ``owner_name`` there
    yields a nameless "Estate of ". Order: owner_name (already FIRST LAST) →
    decedent_name → tax_owner_name converted via ``parse_tax_owner_name``.

    Returns "" when no usable person name exists (business/unparseable). The
    caller keeps prior behavior in that case rather than fabricating a name —
    per NAMELF rules we never route an unparseable NAMELF string in as a person.
    """
    person = (getattr(notice, "owner_name", "") or "").strip()
    if not person:
        person = (getattr(notice, "decedent_name", "") or "").strip()
    if not person:
        raw = (getattr(notice, "tax_owner_name", "") or "").strip()
        if raw:
            parsed = parse_tax_owner_name(raw)
            if parsed:
                person = parsed[0]
    return person


def _parse_notice_owner_name(raw: str) -> list[str]:
    """Parse owner_name from notice text (already in FIRST [MIDDLE] LAST order).

    Unlike parse_tax_owner_name (which handles LAST FIRST MIDDLE tax API format),
    notice owner names are in natural order and just need cleanup + splitting.

    Examples:
        "DEBRA BELL" → ["Debra Bell"]
        "MICHAEL BRANDON HASTING" → ["Michael Brandon Hasting"]
        "STEPHEN D. HANSON AND CHELSEA HANSON" → ["Stephen D. Hanson", "Chelsea Hanson"]
        "BRANDY N. HUMPHREY AND HUSBAND MICHAEL A. HUMPHREY, TENANTS BY THE ENTIRETY"
            → ["Brandy N. Humphrey", "Michael A. Humphrey"]
    """
    if not raw or not raw.strip():
        return []

    name = raw.strip()

    # Extract personal name from trust/estate structures before business check
    personal = _extract_personal_from_trust_estate(name)
    if personal:
        name = personal

    # Skip business entities
    if _BUSINESS_RE.search(name):
        return []

    # Strip notice-specific suffixes (Tenants By The Entirety, etc.)
    name = _NOTICE_SUFFIX_RE.sub("", name).strip()
    # Strip tax-API suffixes too (LIFE EST, ETAL, etc.)
    name = _SUFFIX_RE.sub("", name).strip()

    # Split on " AND " (word) or "&" (symbol)
    parts = re.split(r"\s+AND\s+|\s*&\s*", name, flags=re.IGNORECASE)

    results = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Strip relational prefixes: "Husband ", "Wife ", "Spouse "
        part = re.sub(r"^\s*(?:husband|wife|spouse)\s+", "", part, flags=re.IGNORECASE).strip()
        if part:
            results.append(part.title())

    return [r for r in results if r]


def _is_obituary_url(url: str) -> bool:
    """Check if a URL is from a known obituary site."""
    url_lower = url.lower()
    for domain in OBITUARY_DOMAINS:
        if domain in url_lower:
            return True
    # Also match generic obituary URL patterns
    if "/obituar" in url_lower or "/memorial" in url_lower:
        return True
    return False


# DDGS wraps primp (a Rust HTTP lib) which deadlocks under concurrent calls
# from multiple Python threads. Serialize the search itself with this lock;
# the expensive work (page fetch + LLM parse) still runs in parallel.
_ddgs_lock = threading.Lock()


def _serper_search(query: str, max_results: int = 8) -> list[dict]:
    """Serper.dev (paid Google API). Items normalized to the DDGS shape."""
    import config as cfg  # noqa: PLC0415
    if not cfg.SERPER_API_KEY:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": cfg.SERPER_API_KEY,
                     "Content-Type": "application/json"},
            json={"q": query, "num": max_results},
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {"href": it.get("link", ""), "title": it.get("title", ""),
             "body": it.get("snippet", "")}
            for it in resp.json().get("organic", [])
        ]
    except Exception as e:
        logger.debug("Serper search failed for '%s': %s", query, e)
        return []


def _ddgs_search(query: str, max_results: int = 8) -> list[dict]:
    """Free DuckDuckGo/Google/Brave via the ddgs lib (primp under the hood)."""
    try:
        with _ddgs_lock:
            return DDGS().text(query, max_results=max_results,
                               backend="google,duckduckgo,brave")
    except Exception as e:
        logger.debug("DDGS search failed for '%s': %s", query, e)
        return []


def _web_search(query: str, max_results: int = 8) -> list[dict]:
    """Web search: Serper.dev-first when a key is configured, DDGS fallback.

    DuckDuckGo/DDGS free backends are routinely IP-blocked from datacenter
    ranges (Apify): every request ConnectTimeouts across all 3 backends before
    giving up, wasting seconds per search *and* returning zero results — which
    then reads as "owner not deceased". Serper is fast + reliable, so when a
    SERPER_API_KEY is set we hit it first and only fall back to the free
    backends if Serper is unset or errors. Behaviour is overridable:
      OBIT_SEARCH_BACKEND=serper  → Serper only (skip DDGS)
      OBIT_SEARCH_BACKEND=ddg     → DDGS only (skip Serper; free)
    Serper items are normalized to the DDGS shape (href/title/body).
    """
    import config as cfg  # noqa: PLC0415
    backend = os.environ.get("OBIT_SEARCH_BACKEND", "").strip().lower()

    if backend == "serper":
        return _serper_search(query, max_results)
    if backend in ("ddg", "ddgs", "duckduckgo"):
        return _ddgs_search(query, max_results)

    # Auto (default): Serper-first when configured, DDGS as free fallback.
    if cfg.SERPER_API_KEY:
        results = _serper_search(query, max_results)
        if results:
            return results
        return _ddgs_search(query, max_results)
    return _ddgs_search(query, max_results)


def _search_obituary(name: str, city: str, extra_terms: str = "") -> list[dict]:
    """Search DuckDuckGo for obituary pages matching the person.

    Args:
        name: Person's full name.
        city: City for geo-filtering (empty string to omit).
        extra_terms: Additional search terms to replace "obituary" keyword
                     (e.g. '"death notice" OR "funeral"').

    Returns list of {url, title, snippet} for obituary-domain results.
    """
    keyword = extra_terms if extra_terms else "obituary"
    query = f'{name} {keyword} Texas' if not city else f'{name} {keyword} {city} Texas'

    results = _web_search(query, 8)
    if not results:
        return []

    obituary_results = []
    for r in results:
        url = r.get("href", "")
        if _is_obituary_url(url):
            obituary_results.append({
                "url": url,
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
            })

    # Also include non-obituary-domain results that mention "obituary" in title/snippet
    for r in results:
        url = r.get("href", "")
        if url in [o["url"] for o in obituary_results]:
            continue
        title = r.get("title", "").lower()
        snippet = r.get("body", "").lower()
        if ("obituary" in title or "obituary" in snippet or "passed away" in snippet
                or "death notice" in title or "death notice" in snippet
                or "funeral" in title or "funeral" in snippet):
            obituary_results.append({
                "url": url,
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
            })

    return obituary_results[:8]  # Process all DDG results (was 5, raised for coverage)


def _extract_structured_text(html: str, url: str) -> str:
    """Extract obituary text from structured data in React SPA pages.

    legacy.com renders client-side — BeautifulSoup gets ~75 chars from a 390KB
    page. The actual obituary text lives in:
    1. JSON-LD (<script type="application/ld+json">) → articleBody
    2. window.__INITIAL_STATE__ → personStore.displayText.text
    """
    # Method 1: JSON-LD
    try:
        for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            try:
                data = json.loads(match.group(1))
                # Handle both single object and array
                items = data if isinstance(data, list) else [data]
                for item in items:
                    article_body = ""
                    if item.get("@type") in ("NewsArticle", "Article", "Obituary"):
                        article_body = item.get("articleBody", "")
                    elif item.get("@type") == "Person" and item.get("description"):
                        article_body = item.get("description", "")
                    if article_body and len(article_body) >= 100:
                        # Clean HTML tags from articleBody
                        text = re.sub(r"<br\s*/?>", "\n", article_body)
                        text = re.sub(r"<[^>]+>", "", text)
                        text = re.sub(r"\n{3,}", "\n\n", text)
                        logger.debug("JSON-LD extracted %d chars from %s", len(text), url)
                        return text.strip()
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
    except Exception:
        pass

    # Method 2: window.__INITIAL_STATE__ (legacy.com old format)
    try:
        state_match = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|window\.)",
            html, re.DOTALL,
        )
        if state_match:
            state = json.loads(state_match.group(1))
            # Navigate: personStore.displayText.text
            person_store = state.get("personStore", {})
            display_text = person_store.get("displayText", {})
            text = display_text.get("text", "")
            if text and len(text) >= 100:
                text = re.sub(r"<br\s*/?>", "\n", text)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
                logger.debug("__INITIAL_STATE__ extracted %d chars from %s", len(text), url)
                return text.strip()
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    except Exception:
        pass

    return ""


def _fetch_cached_text(url: str) -> str:
    """Fetch obituary text from Google Cache or Wayback Machine.

    Called when primary fetch gets a 403 (e.g., DignityMemorial.com).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    # Try Wayback Machine
    try:
        wb_url = f"https://web.archive.org/web/2/{url}"
        resp = requests.get(wb_url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code == 200:
            content = resp.text[:200000]
            # Try structured extraction first
            structured = _extract_structured_text(content, wb_url)
            if structured and len(structured) >= 100:
                logger.debug("Wayback Machine extracted %d chars for %s", len(structured), url)
                return structured[:MAX_OBITUARY_TEXT]
            # BeautifulSoup fallback
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            if len(text) >= 100:
                logger.debug("Wayback Machine BS4 extracted %d chars for %s", len(text), url)
                return text[:MAX_OBITUARY_TEXT]
    except Exception as e:
        logger.debug("Wayback Machine fetch failed for %s: %s", url, e)

    return ""


def _is_listing_url(url: str) -> bool:
    """Check if a URL is a generic listing page rather than a specific obituary."""
    url_lower = url.lower()
    # legacy.com listing pages: /us/obituaries/local/tennessee/...
    if "legacy.com" in url_lower and "/local/" in url_lower:
        return True
    # Generic search/listing patterns
    if any(p in url_lower for p in ["/search?", "/results?", "/browse/", "/listing"]):
        return True
    return False


def _refetch_specific_obituary(
    name: str,
    city: str,
    original_url: str,
    api_key: str,
    address: str = "",
) -> tuple[dict | None, str, str]:
    """Re-search for a specific obituary page when original match was a listing page.

    Returns (parsed, url, source_type) or (None, "", "") if unsuccessful.
    """
    queries = [
        f'"{name}" obituary site:legacy.com',
        f'"{name}" obituary {city} Texas "survived by"',
    ]

    for query in queries:
        try:
            results = _web_search(query, 5)
        except Exception:
            continue

        for r in results:
            url = r.get("href", "")
            if not url or not _is_obituary_url(url):
                continue
            if _is_listing_url(url):
                continue
            if url == original_url:
                continue

            page_text = _fetch_page_text(url)
            if not page_text or len(page_text) < 100:
                continue

            parsed = _parse_obituary_with_llm(
                obituary_text=page_text,
                owner_name=name,
                city=city,
                address=address,
                api_key=api_key,
            )
            if parsed and parsed.get("confidence") in ("high", "medium"):
                logger.info("  Re-search found specific obituary: %s", url)
                return parsed, url, "full_page"

        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    return None, "", ""


AGGRESSIVE_SURVIVOR_PROMPT = """\
This is an obituary for {owner_name}. List every person named in the text who \
appears to still be alive (a survivor, not someone who predeceased them).

Give each person's name exactly as written in the text. Do NOT append or guess a surname \
for someone named by first name only — return the name verbatim or omit it; never invent a \
person who is not literally named. Infer the relationship from context words like wife, husband, \
son, daughter, brother, sister, child, grandchild, stepson, stepdaughter, niece, nephew, \
parent, mother, father, in-law, etc. If the relationship is ambiguous, use "family_member" \
rather than omitting the person.

Return a JSON object with ONE key:
- "survivors": array of objects, each with "name" (string) and "relationship" (string)

Return an empty array if no living persons are named.

Obituary text:
{obituary_text}"""


SNIPPET_SURVIVOR_PROMPT = """\
I have confirmed this property owner is deceased. Extract ANY family member \
names from these search result snippets.

Owner name: {owner_name}
Property city: {city}

Look for: names after "survived by", "wife", "husband", "son", "daughter", \
"brother", "sister", "mother", "father". Give each name exactly as written — do NOT \
append or guess a surname for a first-name-only mention, and never invent a person not \
literally named in the snippets. Use best-guess relationship if not explicit.

Return a JSON object with:
- "survivors": array of objects with "name" and "relationship" keys
- "executor_named": empty string (not available from snippets)
- "confidence": "high" if clear survivor names found, "low" otherwise

Search snippets:
{snippets}"""


def _search_survivors_targeted(
    name: str,
    city: str,
    api_key: str,
) -> list[dict]:
    """Run targeted searches for survivor names when standard snippet lacks them.

    Returns list of survivor dicts [{name, relationship}] or empty list.
    """
    queries = [
        f'"{name}" "survived by" {city} Texas',
        f'"{name}" "preceded in death" {city} Texas',
        f'"{name}" obituary wife OR husband OR son OR daughter {city}',
        f'"{name}" funeral OR memorial service {city} Texas',
    ]

    all_snippets = []
    for query in queries:
        try:
            results = _web_search(query, 5)
            for r in results:
                snippet = r.get("body", "")
                title = r.get("title", "")
                if snippet:
                    all_snippets.append(f"Title: {title}\nSnippet: {snippet}")
        except Exception:
            continue
        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    if not all_snippets:
        return []

    combined = "\n\n".join(all_snippets[:6])
    prompt = SNIPPET_SURVIVOR_PROMPT.format(
        owner_name=name,
        city=city or "unknown",
        snippets=combined,
    )

    try:
        parsed = llm_client.chat_json(
            prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS,
            api_key=api_key, model=_obituary_model(),
        )
        if not parsed:
            return []
        # Grounding guard: only keep survivors actually named in the snippets searched.
        survivors = _validate_survivors_against_text(
            parsed.get("survivors", []), combined, name,
        )
        confidence = parsed.get("confidence", "")
        if survivors and confidence == "high":
            logger.info("  Targeted snippet: %d survivors (high conf) for %s", len(survivors), name)
            return survivors
        if survivors and confidence == "medium":
            # Accept medium-confidence if every survivor has both name and relationship
            all_complete = all(
                s.get("name", "").strip() and s.get("relationship", "").strip()
                for s in survivors
            )
            if all_complete:
                logger.info(
                    "  Targeted snippet: %d survivors (medium conf, all complete) for %s",
                    len(survivors), name,
                )
                return survivors
            logger.debug(
                "  Targeted snippet: medium confidence but incomplete survivor data for %s",
                name,
            )
    except Exception as e:
        logger.debug("Targeted survivor search LLM failed for %s: %s", name, e)

    return []


def _extract_survivors_aggressive(
    obituary_text: str,
    owner_name: str,
    api_key: str,
) -> list[dict]:
    """Aggressive fallback: extract any named living persons from obituary text.

    Called when standard LLM extraction returned empty survivors[] from a
    confirmed full-page obituary. Uses a permissive prompt with no confidence
    requirement. All returned survivors are treated as 'unverified'.
    """
    if not obituary_text or not obituary_text.strip() or not api_key:
        return []

    prompt = AGGRESSIVE_SURVIVOR_PROMPT.format(
        owner_name=owner_name,
        obituary_text=obituary_text[:MAX_OBITUARY_TEXT],
    )

    try:
        parsed = llm_client.chat_json(
            prompt, system=SYSTEM_PROMPT, max_tokens=512,
            api_key=api_key, model=_obituary_model(),
        )
        if not parsed:
            return []
        survivors = parsed.get("survivors", [])
        survivors = [s for s in survivors if s.get("name", "").strip()]
        # Grounding guard: this permissive prompt is the most likely to confabulate.
        survivors = _validate_survivors_against_text(survivors, obituary_text, owner_name)
        if survivors:
            logger.info(
                "  Aggressive extraction found %d survivor(s) for %s",
                len(survivors), owner_name,
            )
        return survivors
    except Exception as e:
        logger.debug("Aggressive survivor extraction failed for %s: %s", owner_name, e)
        return []


# ── DM address lookup ──────────────────────────────────────────────

PEOPLE_SEARCH_DOMAINS = {
    "truepeoplesearch.com",
    "fastpeoplesearch.com",
    "cyberbackgroundchecks.com",
    "peoplefinder.com",
    "spokeo.com",
    "whitepages.com",
}

ADDRESS_EXTRACT_PROMPT = """\
Extract the current residential mailing address for this person from the web page text.

Person: {name}
Expected area: {city}, Texas (or nearby)

Instructions:
1. The page may list MULTIPLE people. Scan ALL result blocks to find the one that \
best matches "{name}" in {city}, Texas.
2. Within that block, prefer the "Lives at" or "Current address" over "Used to live" addresses.
3. If you find an exact name + state match, return it even if the city differs slightly \
(people move within Texas).
4. If multiple exact matches exist (common name), pick the Texas address closest \
to {city}.
5. If no confident match exists, return empty strings — do not guess.

Return ONLY valid JSON with these exact keys:
- "street": street address (e.g., "1234 Oak Street") — empty string if not found
- "city": city name — empty string if not found
- "state": 2-letter state code — "TX" if Texas
- "zip": 5-digit zip code — empty string if not found
- "confidence": "high" if name+state match found, "medium" if likely match, "low" if uncertain

Web page text:
{page_text}"""


def _lookup_dm_address_cad(name: str) -> dict | None:
    """Search TX County Appraisal District by DM name for a property address.

    TX CAD lookup not yet implemented — placeholder for Travis/Bell/Williamson CAD APIs.
    Returns {street, city, state, zip} or None.
    """
    logger.debug("TX CAD DM address lookup not yet implemented for: %s", name)
    return None


def _lookup_dm_address_web(name: str, city: str, api_key: str) -> dict | None:
    """Search free people search sites for DM's residential address.

    Uses DuckDuckGo to find pages on people search sites, then Claude Haiku
    to extract the address from page content.
    """
    # Targeted people search query
    site_filter = " OR ".join(f"site:{d}" for d in list(PEOPLE_SEARCH_DOMAINS)[:4])
    queries = [
        f'"{name}" {city} Texas {site_filter}',
        f'"{name}" Texas address {city}',
    ]

    for query in queries:
        try:
            results = _web_search(query, 5)
        except Exception:
            time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
            continue

        for r in results:
            url = r.get("href", "")
            url_lower = url.lower()

            # Prioritize known people search domains
            is_people_site = any(d in url_lower for d in PEOPLE_SEARCH_DOMAINS)
            if not is_people_site:
                continue

            page_text = _fetch_page_text(url)
            if not page_text or len(page_text) < 50:
                continue

            # LLM extraction
            prompt = ADDRESS_EXTRACT_PROMPT.format(
                name=name,
                city=city or "Austin",
                page_text=page_text[:MAX_OBITUARY_TEXT],
            )
            try:
                parsed = llm_client.chat_json(prompt, system=SYSTEM_PROMPT, max_tokens=256, api_key=api_key)
                if parsed:
                    street = parsed.get("street", "").strip()
                    if street and parsed.get("confidence") in ("high", "medium"):
                        logger.info("  People search found address for %s: %s, %s",
                                    name, street, parsed.get("city", ""))
                        return {
                            "street": street,
                            "city": parsed.get("city", ""),
                            "state": parsed.get("state", "TX"),
                            "zip": parsed.get("zip", ""),
                        }
            except Exception as e:
                logger.debug("Address LLM extraction failed for %s: %s", name, e)

            time.sleep(random.uniform(0.5, 1.0))

        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    return None


def _build_people_search_urls(name: str, city: str) -> list[str]:
    """Build direct URLs for free people search sites that show addresses.

    CyberBackgroundChecks is the only reliable free site — Firecrawl
    renders it successfully with full address history, phones, and relatives.
    TruePeopleSearch times out and FastPeopleSearch is Cloudflare-blocked.
    Uses first + last name only (no middle name) for best match rates.
    """
    parts = name.strip().split()
    if len(parts) < 2:
        return []
    first = parts[0].lower()
    last = parts[-1].lower()
    city_clean = (city or "Austin").strip().lower().replace(" ", "-")

    urls = [
        # CyberBackgroundChecks — shows full address history, phones, relatives
        f"https://www.cyberbackgroundchecks.com/people/"
        f"{first}-{last}/{city_clean}-tx",
    ]
    return urls


def _search_serper(name: str, city: str) -> list[str]:
    """Search Google via Serper.dev for people search site URLs.

    Returns a list of URLs from known people search domains.
    Falls through gracefully if SERPER_API_KEY is not configured.
    Uses first+last name only to avoid middle name mismatches.
    """
    import config as cfg
    if not cfg.SERPER_API_KEY:
        return []

    parts = name.strip().split()
    if len(parts) < 2:
        return []
    first = parts[0]
    last = parts[-1]

    # CyberBackgroundChecks is the only free site Firecrawl can scrape reliably.
    # TruePeopleSearch times out, FastPeopleSearch is Cloudflare-blocked.
    city_clean = (city or "Austin").strip()
    query = f'"{first} {last}" {city_clean} TX site:cyberbackgroundchecks.com'

    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": cfg.SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": 5},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        urls = []
        for item in data.get("organic", []):
            url = item.get("link", "")
            if any(d in url.lower() for d in PEOPLE_SEARCH_DOMAINS):
                urls.append(url)
        return urls[:3]
    except Exception as e:
        logger.debug("Serper search failed for %s: %s", name, e)
        return []


def _filter_cbc_markdown(markdown: str) -> str:
    """Strip noise from CyberBackgroundChecks markdown to fit more results.

    Removes phone numbers, relatives, associates, and navigation boilerplate.
    Keeps name, age, "Lives at", and "Used to live" sections.
    """
    filtered_lines = []
    skip_section = False
    for line in markdown.split("\n"):
        # Skip phone/relative/associate/navigation sections
        if line.startswith("### Phones") or line.startswith("### Related to") or \
           line.startswith("### Associated with") or line.startswith("### Other observed") or \
           line.startswith("This is me..."):
            skip_section = True
            continue
        # Resume at next person heading or address section
        if line.startswith("## ") or line.startswith("### Lives at") or \
           line.startswith("### Used to live") or line.startswith("[VIEW DETAILS]"):
            skip_section = False
        if skip_section:
            continue
        # Skip boilerplate navigation lines
        if line.startswith("[People Directory]") or line.startswith("[Displaying page") or \
           line.startswith("- [**") or line.startswith("Clear"):
            continue
        filtered_lines.append(line)
    return "\n".join(filtered_lines)


_firecrawl_credits_exhausted = False
_firecrawl_budget_total = int(os.environ.get("FIRECRAWL_BUDGET", "3000"))
_firecrawl_calls_used = 0
_firecrawl_lock = threading.Lock()


def _fetch_firecrawl(
    url: str, wait_ms: int = 5000, max_text: int = 0, priority: str = "high"
) -> str:
    """Fetch a page using Firecrawl API with JS rendering.

    Returns clean markdown text. Falls back to empty string if not configured
    or on error. Uses waitFor to let JS-heavy pages fully render.
    max_text: override truncation limit (0 = use MAX_OBITUARY_TEXT).
    priority: "high" (obituary pages, always allowed), "medium" (Phase B upgrades,
              allowed if >50% budget remains), "low" (DM address lookups, allowed
              if >75% budget remains).
    """
    global _firecrawl_credits_exhausted, _firecrawl_calls_used
    import config as cfg
    if not cfg.FIRECRAWL_API_KEY or _firecrawl_credits_exhausted:
        return ""

    # Budget-based priority gating (thread-safe read)
    with _firecrawl_lock:
        budget_remaining_pct = 1.0 - (_firecrawl_calls_used / max(_firecrawl_budget_total, 1))
        if priority == "medium" and budget_remaining_pct < 0.50:
            logger.debug("Firecrawl budget <50%% — skipping medium-priority fetch for %s", url)
            return ""
        if priority == "low" and budget_remaining_pct < 0.75:
            logger.debug("Firecrawl budget <75%% — skipping low-priority fetch for %s", url)
            return ""
        _firecrawl_calls_used += 1

    limit = max_text or MAX_OBITUARY_TEXT
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {cfg.FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"], "waitFor": wait_ms},
            timeout=45,
        )
        if resp.status_code == 402:
            with _firecrawl_lock:
                _firecrawl_credits_exhausted = True
            logger.warning(
                "Firecrawl credits exhausted after %d calls — falling back to other methods",
                _firecrawl_calls_used,
            )
            return ""
        resp.raise_for_status()
        data = resp.json()
        markdown = data.get("data", {}).get("markdown", "")
        if not markdown:
            return ""
        # Filter CBC pages to strip noise and fit more results
        if "cyberbackgroundchecks.com" in url:
            markdown = _filter_cbc_markdown(markdown)
        if _firecrawl_calls_used % 50 == 0:
            logger.info(
                "Firecrawl budget: %d/%d used (%.0f%% remaining)",
                _firecrawl_calls_used, _firecrawl_budget_total, budget_remaining_pct * 100,
            )
        return markdown[:limit]
    except Exception as e:
        logger.debug("Firecrawl fetch failed for %s: %s", url, e)
        return ""


def _extract_address_from_page(
    page_text: str, name: str, city: str, api_key: str
) -> dict | None:
    """Use Claude Haiku to extract a mailing address from page text."""
    prompt = ADDRESS_EXTRACT_PROMPT.format(
        name=name,
        city=city or "Austin",
        page_text=page_text[:MAX_ADDRESS_TEXT],
    )
    try:
        parsed = llm_client.chat_json(prompt, system=SYSTEM_PROMPT, max_tokens=256, api_key=api_key)
        if not parsed:
            return None

        street = parsed.get("street", "").strip()
        if street and parsed.get("confidence") in ("high", "medium"):
            return {
                "street": street,
                "city": parsed.get("city", ""),
                "state": parsed.get("state", "TX"),
                "zip": parsed.get("zip", ""),
            }
    except Exception as e:
        logger.debug("Address LLM extraction failed for %s: %s", name, e)
    return None


def _lookup_dm_address_serper_firecrawl(
    name: str, city: str, api_key: str
) -> dict | None:
    """Look up DM address via direct people search URLs + Firecrawl rendering.

    Tier 2: First tries direct TruePeopleSearch/FastPeopleSearch URLs (which
    show actual street addresses for free), then falls back to Serper Google
    search for additional people search sites. Uses Claude Haiku to extract
    the address from rendered page content.
    """
    # Phase 1: Direct people search URLs (no Google search needed)
    direct_urls = _build_people_search_urls(name, city)
    for url in direct_urls:
        page_text = _fetch_firecrawl(url, max_text=MAX_ADDRESS_TEXT, priority="low")
        if not page_text or len(page_text) < 100:
            page_text = _fetch_page_text(url)
        if not page_text or len(page_text) < 100:
            continue

        result = _extract_address_from_page(page_text, name, city, api_key)
        if result:
            logger.debug("Direct URL hit for %s: %s", name, url)
            return result
        time.sleep(random.uniform(0.5, 1.0))

    # Phase 2: Serper Google search fallback
    serper_urls = _search_serper(name, city)
    for url in serper_urls:
        # Skip URLs we already tried via direct
        if any(url.startswith(d.rsplit("/", 1)[0]) for d in direct_urls):
            continue

        page_text = _fetch_firecrawl(url, max_text=MAX_ADDRESS_TEXT, priority="low")
        if not page_text or len(page_text) < 100:
            page_text = _fetch_page_text(url)
        if not page_text or len(page_text) < 100:
            continue

        result = _extract_address_from_page(page_text, name, city, api_key)
        if result:
            logger.debug("Serper URL hit for %s: %s", name, url)
            return result
        time.sleep(random.uniform(0.5, 1.0))

    return None


def _lookup_dm_address_tracerfy(name: str, city: str,
                                 address: str = "", zip_code: str = "") -> dict | None:
    """Look up DM mailing address via Tracerfy Instant Trace API.

    Uses POST /v1/api/trace/lookup/ (synchronous, single-record).
    Cost: 5 credits ($0.10) per hit, 0 on miss. Rate limit: 500 RPM.
    """
    import config as cfg

    if not cfg.TRACERFY_API_KEY:
        return None

    # Split name into first/last
    parts = name.strip().split()
    if len(parts) < 2:
        return None
    first_name = parts[0]
    last_name = parts[-1]

    try:
        resp = requests.post(
            "https://tracerfy.com/v1/api/trace/lookup/",
            headers={
                "Authorization": f"Bearer {cfg.TRACERFY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "address": address or "",
                "city": city or "",
                "state": "TX",
                "zip": zip_code or "",
                "find_owner": False,
                "first_name": first_name,
                "last_name": last_name,
            },
            timeout=30,
        )
        if resp.status_code == 402:
            # Only surface this as WARNING once per run; subsequent calls
            # in the same process will still hit 402 and repeat the message.
            logger.warning(
                "Tracerfy instant 402 for %s — INSUFFICIENT CREDITS: %s",
                name, resp.text[:200],
            )
        elif resp.status_code != 200:
            logger.debug("Tracerfy instant %d for %s: %s",
                         resp.status_code, name, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()

        if not data.get("hit") or not data.get("persons"):
            return None

        person = data["persons"][0]
        mail = person.get("mailing_address") or {}
        street = (mail.get("street") or "").strip()
        if street:
            return {
                "street": street,
                "city": (mail.get("city") or "").strip(),
                "state": (mail.get("state") or "TX").strip(),
                "zip": (mail.get("zip") or "").strip(),
            }
        return None
    except Exception as e:
        logger.debug("Tracerfy instant lookup failed for %s: %s", name, e)
        return None


def _batch_tracerfy_lookup(notices: list) -> None:
    """Batch skip-trace all notices that still need a DM mailing address.

    Submits a single CSV with all DM names to Tracerfy, polls for results,
    and updates NoticeData fields in-place. Much faster than per-record calls.
    """
    import config as cfg
    import csv as csv_mod
    import io

    if not cfg.TRACERFY_API_KEY or not notices:
        return

    # Build batch CSV — Tracerfy requires address data for skip tracing
    csv_buffer = io.StringIO()
    writer = csv_mod.writer(csv_buffer)
    writer.writerow(["first_name", "last_name", "address", "city", "state",
                      "zip", "mail_address", "mail_city", "mail_state"])

    lookup_map: list[tuple] = []  # [(notice, first_name, last_name), ...]
    for n in notices:
        parts = n.decision_maker_name.strip().split()
        if len(parts) < 2:
            continue
        first_name = parts[0]
        last_name = parts[-1]
        # Use property address as the known address for skip tracing
        addr = n.address.strip()
        city_hint = n.city.strip() or "Austin"
        zip_code = n.zip.strip()
        writer.writerow([first_name, last_name, addr, city_hint, "TX",
                         zip_code, "", "", ""])
        lookup_map.append((n, first_name, last_name))

    if not lookup_map:
        return

    csv_content = csv_buffer.getvalue()
    csv_buffer.close()

    try:
        resp = requests.post(
            "https://tracerfy.com/v1/api/trace/",
            headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"},
            data={
                "first_name_column": "first_name",
                "last_name_column": "last_name",
                "address_column": "address",
                "city_column": "city",
                "state_column": "state",
                "zip_column": "zip",
                "mail_address_column": "mail_address",
                "mail_city_column": "mail_city",
                "mail_state_column": "mail_state",
            },
            files={"csv_file": ("dm_batch.csv", csv_content, "text/csv")},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.debug("Tracerfy batch %d response: %s",
                         resp.status_code, resp.text[:500])
        resp.raise_for_status()
        queue_data = resp.json()
        queue_id = queue_data.get("queue_id")
        if not queue_id:
            logger.warning("Tracerfy batch returned no queue_id")
            return

        logger.info("  Tracerfy batch job %s submitted (%d records)", queue_id, len(lookup_map))

        # Poll for results — batch jobs take longer
        # GET /queue/:id may return a list (records) or dict (status wrapper)
        for _attempt in range(30):
            time.sleep(5)
            result_resp = requests.get(
                f"https://tracerfy.com/v1/api/queue/{queue_id}",
                headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"},
                timeout=15,
            )
            result_resp.raise_for_status()
            result_data = result_resp.json()

            # Handle both response formats
            if isinstance(result_data, list):
                records = result_data
            elif isinstance(result_data, dict):
                status = result_data.get("status", "")
                if status == "failed":
                    logger.warning("Tracerfy batch job %s failed", queue_id)
                    return
                if status != "completed":
                    continue  # still pending
                records = result_data.get("records", [])
            else:
                continue

            matched = 0
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                street = (rec.get("mail_address") or "").strip()
                if not street:
                    continue
                # Match back to notice by first+last name
                rec_first = (rec.get("first_name") or "").strip().lower()
                rec_last = (rec.get("last_name") or "").strip().lower()
                for notice, first, last in lookup_map:
                    if (first.lower() == rec_first
                            and last.lower() == rec_last
                            and not notice.decision_maker_street):
                        notice.decision_maker_street = street
                        notice.decision_maker_city = (rec.get("mail_city") or "").strip()
                        notice.decision_maker_state = (rec.get("mail_state") or "TX").strip()
                        notice.decision_maker_zip = (rec.get("mail_zip") or "").strip()
                        matched += 1
                        logger.info(
                            "    Tracerfy batch: %s -> %s, %s %s",
                            notice.decision_maker_name, street,
                            notice.decision_maker_city, notice.decision_maker_state,
                        )
                        break
            logger.info("  Tracerfy batch complete: %d/%d matched", matched, len(lookup_map))
            return

        logger.warning("Tracerfy batch job %s timed out after 150s", queue_id)
    except Exception as e:
        logger.warning("Tracerfy batch lookup failed: %s", e)


def _lookup_dm_address(
    name: str, city: str, api_key: str, tracerfy_tier1: bool = False,
) -> dict:
    """Look up decision-maker's mailing address using tiered sources.

    Tier 0 (opt-in): Tracerfy skip tracing (paid, highest hit rate)
    Tier 1: TX CAD lookup (not yet implemented)
    Tier 2: Serper.dev + Firecrawl + LLM (cheap, national)
    Tier 2b: DuckDuckGo fallback (free, unreliable -- used when Serper not configured)

    Returns {street, city, state, zip, source} (may have empty values).
    """
    result = {"street": "", "city": "", "state": "", "zip": "", "source": ""}

    if not name or not name.strip():
        return result

    # Tier 0 (opt-in): Tracerfy as primary lookup
    if tracerfy_tier1:
        import config as cfg
        if cfg.TRACERFY_API_KEY:
            tf_result = _lookup_dm_address_tracerfy(
                name, city or "Austin", address="", zip_code=""
            )
            if tf_result and tf_result.get("street"):
                result.update(tf_result)
                result["source"] = "tracerfy_tier1"
                logger.info("    Tier 0 (Tracerfy): %s, %s",
                            result["street"], result["city"])
                return result

    # Tier 1: TX CAD lookup (not yet implemented)
    cad_result = _lookup_dm_address_cad(name)
    if cad_result and cad_result.get("street"):
        result.update(cad_result)
        result["source"] = "tx_cad"
        logger.info("    Tier 1 (TX CAD): %s", result["street"])
        return result

    # Tier 2: Direct people search URLs + Firecrawl + LLM
    import config as cfg
    sf_result = _lookup_dm_address_serper_firecrawl(
        name, city or "Austin", api_key
    )
    if sf_result and sf_result.get("street"):
        result.update(sf_result)
        result["source"] = "people_search"
        logger.info("    Tier 2 (People Search): %s, %s",
                    result["street"], result["city"])
        return result

    # Tier 2b: DuckDuckGo fallback (when Serper/Firecrawl not configured)
    if not cfg.SERPER_API_KEY and not cfg.FIRECRAWL_API_KEY:
        web_result = _lookup_dm_address_web(name, city or "Austin", api_key)
        if web_result and web_result.get("street"):
            result.update(web_result)
            result["source"] = "ddg_people_search"
            logger.info("    Tier 2b (DDG): %s, %s",
                        result["street"], result["city"])
            return result

    return result


# KNOX_TAX_API removed — TX CAD APIs not yet implemented
REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0


def _fetch_page_text(url: str) -> str:
    """Fetch a URL and extract readable text content."""
    # Check if domain is known to block direct HTTP — skip straight to Firecrawl
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().replace("www.", "")
    if any(blocked in domain for blocked in KNOWN_403_DOMAINS):
        logger.debug("Known-403 domain %s — skipping HTTP, trying Firecrawl first", domain)
        fc_text = _fetch_firecrawl(url)
        if fc_text and len(fc_text) >= 100:
            return fc_text
        return _fetch_cached_text(url)

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()

        # Increased limit for JSON-heavy React SPA pages
        content = resp.text[:200000]

        # Try structured data extraction first (JSON-LD, __INITIAL_STATE__)
        structured = _extract_structured_text(content, url)
        if structured and len(structured) >= 100:
            return structured[:MAX_OBITUARY_TEXT]

        # BeautifulSoup fallback for non-SPA sites
        soup = BeautifulSoup(content, "html.parser")

        # Remove script and style elements
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Collapse excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:MAX_OBITUARY_TEXT]

    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logger.debug("403 blocked for %s, trying Firecrawl", url)
            # Firecrawl can render JS and bypass many 403 blocks
            fc_text = _fetch_firecrawl(url)
            if fc_text and len(fc_text) >= 100:
                logger.debug("Firecrawl fetched %d chars for %s", len(fc_text), url)
                return fc_text
            logger.debug("Firecrawl failed, trying cached sources for %s", url)
            return _fetch_cached_text(url)
        logger.debug("Failed to fetch %s: %s", url, e)
        return ""
    except requests.exceptions.ReadTimeout:
        # Timeout — try Firecrawl, then Wayback Machine
        logger.debug("Timeout for %s, trying Firecrawl", url)
        fc_text = _fetch_firecrawl(url)
        if fc_text and len(fc_text) >= 100:
            return fc_text
        logger.debug("Firecrawl failed on timeout, trying cached sources for %s", url)
        return _fetch_cached_text(url)
    except Exception as e:
        logger.debug("Failed to fetch %s: %s", url, e)
        return ""


def _parse_obituary_with_llm(
    obituary_text: str,
    owner_name: str,
    city: str,
    address: str,
    api_key: str,
) -> dict | None:
    """Use Claude Haiku to validate and parse an obituary.

    Returns parsed dict if the obituary matches the owner, None otherwise.
    """
    if not obituary_text.strip():
        return None
    # For Ollama backend, api_key not required
    import config as _cfg
    if getattr(_cfg, "LLM_BACKEND", "anthropic") == "anthropic" and not api_key:
        return None

    prompt = OBITUARY_PROMPT.format(
        owner_name=owner_name,
        city=city or "unknown",
        address=address or "unknown",
        obituary_text=obituary_text[:MAX_OBITUARY_TEXT],
    )

    try:
        parsed = llm_client.chat_json(
            prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS,
            api_key=api_key, model=_obituary_model(),
        )
        if not parsed:
            return None

        if not parsed.get("match"):
            return None

        # Validate minimum fields
        if not parsed.get("full_name"):
            return None

        # Grounding guard: drop any survivor / predeceased / executor that is not
        # literally named in the obituary text (defends against hallucinated heirs).
        deceased = parsed.get("full_name") or owner_name
        parsed["survivors"] = _validate_survivors_against_text(
            parsed.get("survivors", []), obituary_text, deceased,
        )
        if parsed.get("preceded_in_death"):
            parsed["preceded_in_death"] = _validate_survivors_against_text(
                parsed.get("preceded_in_death", []), obituary_text, deceased,
            )
        executor = (parsed.get("executor_named") or "").strip()
        if executor and not _validate_survivors_against_text(
            [executor], obituary_text, deceased,
        ):
            logger.warning("Grounding guard cleared hallucinated executor_named: %s", executor)
            parsed["executor_named"] = ""

        return parsed

    except Exception as e:
        logger.debug("Obituary LLM parsing failed: %s", e)
        return None


def identify_decision_maker(survivors: list[dict]) -> tuple[str, str]:
    """Identify the decision-maker from a list of survivors.

    Priority (from SKILL.md):
    1. Named executor (if mentioned in survivors)
    2. Surviving spouse
    3. Oldest child (first listed son/daughter)
    4. Sibling (if no spouse/children)
    5. First listed survivor (fallback)

    Returns (name, relationship) tuple.
    """
    if not survivors:
        return ("", "")

    spouse = None
    children = []
    siblings = []
    others = []

    spouse_terms = {"wife", "husband", "spouse", "partner"}
    child_terms = {"son", "daughter", "child", "stepson", "stepdaughter"}
    sibling_terms = {"brother", "sister", "sibling"}

    for s in survivors:
        name = s.get("name", "").strip()
        rel = s.get("relationship", "").strip().lower()
        if not name:
            continue

        # Check for executor
        if "executor" in rel or "personal representative" in rel:
            return (name, "executor")

        if any(t in rel for t in spouse_terms):
            spouse = (name, rel)
        elif any(t in rel for t in child_terms):
            children.append((name, rel))
        elif any(t in rel for t in sibling_terms):
            siblings.append((name, rel))
        else:
            others.append((name, rel))

    if spouse:
        return spouse
    if children:
        return children[0]
    if siblings:
        return siblings[0]
    if others:
        return others[0]
    return ("", "")


# ── Deep prospecting: ranked DMs, heir verification, error map ────────


def rank_decision_makers(
    survivors: list[dict],
    executor_name: str = "",
    heir_statuses: dict[str, str] | None = None,
) -> list[dict]:
    """Rank all survivors as potential decision-makers and classify signing authority.

    Returns list of dicts sorted by priority, each with:
        name, relationship, rank, status, source, signing_authority

    Priority: executor > spouse > children > siblings > others.
    Within each group: verified_living first, then unverified, then deceased.

    Signing authority is based on Texas intestate succession (Texas Estates Code):
      - Executor/PR: always has signing authority
      - Spouse: always has signing authority
      - Children (NOT step-children): signing authority if alive
      - Grandchildren: only if their parent (deceased's child) is also deceased
      - Parents: only if no living spouse AND no living children
      - Siblings: only if no living spouse, children, OR parents
      - In-laws, step-children, friends: never have signing authority
    """
    if not survivors and not executor_name:
        return []

    statuses = heir_statuses or {}

    # ── Relationship term sets ──
    spouse_terms = {"wife", "husband", "spouse", "partner"}
    child_terms = {"son", "daughter", "child", "stepson", "stepdaughter"}
    sibling_terms = {"brother", "sister", "sibling"}
    # Extended sets for signing authority classification
    stepchild_terms = {"stepson", "stepdaughter", "step-son", "step-daughter"}
    inlaw_terms = {
        "mother-in-law", "father-in-law", "sister-in-law", "brother-in-law",
        "son-in-law", "daughter-in-law", "in-law",
    }
    parent_terms = {"mother", "father", "parent"}
    grandchild_terms = {"grandson", "granddaughter", "grandchild"}
    # Spouses of relatives — non-inheriting (e.g., "grandchild's spouse", "grandson's wife")
    spouse_of_patterns = {"'s spouse", "'s wife", "'s husband", "'s partner"}

    executors: list[dict] = []
    spouses: list[dict] = []
    children: list[dict] = []
    siblings: list[dict] = []
    parents: list[dict] = []
    grandchildren: list[dict] = []
    others: list[dict] = []

    # Named executor (may not be in survivors list)
    if executor_name:
        status = statuses.get(executor_name, "unverified")
        executors.append({
            "name": executor_name,
            "relationship": "executor",
            "status": status,
            "source": "obituary_survivors",
        })

    seen_names = {executor_name.lower()} if executor_name else set()

    for s in survivors:
        name = s.get("name", "").strip()
        rel = s.get("relationship", "").strip().lower()
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())

        status = statuses.get(name, "unverified")
        entry = {
            "name": name,
            "relationship": rel,
            "status": status,
            "source": "obituary_survivors",
        }
        # Carry the "not chased with an LLM call" marker through to the heir map
        # so Notes can label these survivors as captured-only (see build_heir_map).
        if s.get("verification_skipped"):
            entry["verification_skipped"] = True

        # Check in-laws FIRST (before child_terms, since "daughter-in-law" contains "daughter")
        if any(t in rel for t in inlaw_terms) or "in-law" in rel or "in law" in rel:
            others.append(entry)
        elif any(p in rel for p in spouse_of_patterns):
            others.append(entry)  # Spouse of a relative — no inheritance rights
        elif "executor" in rel or "personal representative" in rel:
            executors.append(entry)
        elif any(t in rel for t in spouse_terms):
            spouses.append(entry)
        elif any(t in rel for t in stepchild_terms):
            others.append(entry)  # Step-children go to others (no signing authority)
        elif any(t in rel for t in grandchild_terms):
            grandchildren.append(entry)
        elif any(t in rel for t in child_terms):
            children.append(entry)
        elif any(t in rel for t in parent_terms):
            parents.append(entry)
        elif any(t in rel for t in sibling_terms):
            siblings.append(entry)
        else:
            others.append(entry)

    # Build ranked list: within each priority group, living > unverified > deceased
    def _sort_by_status(group: list[dict]) -> list[dict]:
        living = [e for e in group if e["status"] == "verified_living"]
        unverified = [e for e in group if e["status"] == "unverified"]
        deceased = [e for e in group if e["status"] == "deceased"]
        return living + unverified + deceased

    ranked: list[dict] = []
    for group in [executors, spouses, children, grandchildren, parents, siblings, others]:
        ranked.extend(_sort_by_status(group))

    for i, entry in enumerate(ranked):
        entry["rank"] = i + 1

    # ── Pass 2: Determine signing authority (TX intestate succession) ──
    # Check which priority tiers have living/unverified members
    def _has_living(group: list[dict]) -> bool:
        return any(e["status"] in ("verified_living", "unverified") for e in group)

    has_living_spouse = _has_living(spouses)
    has_living_children = _has_living(children)
    has_living_parents = _has_living(parents)

    # Track which children are deceased (for grandchild right of representation)
    deceased_child_names = {
        e["name"].lower() for e in children if e["status"] == "deceased"
    }

    for entry in ranked:
        rel = entry["relationship"].lower()

        # In-laws: NEVER signing authority (check FIRST — "daughter-in-law" contains "daughter")
        if any(t in rel for t in inlaw_terms) or "in-law" in rel or "in law" in rel:
            entry["signing_authority"] = False

        # Spouses of relatives: NEVER signing authority (e.g., "grandchild's spouse")
        elif any(p in rel for p in spouse_of_patterns):
            entry["signing_authority"] = False

        # Step-children: NEVER signing authority (don't inherit intestate)
        elif any(t in rel for t in stepchild_terms):
            entry["signing_authority"] = False

        # Executors/PR: always signing authority
        elif "executor" in rel or "personal representative" in rel:
            entry["signing_authority"] = True

        # Spouse: always signing authority
        elif any(t in rel for t in spouse_terms):
            entry["signing_authority"] = True

        # Grandchildren: only if their parent (a child of deceased) is also deceased
        # Since we can't always trace parent→grandchild, we use a heuristic:
        # if ANY child is deceased, grandchildren MAY have right of representation
        elif any(t in rel for t in grandchild_terms):
            entry["signing_authority"] = bool(deceased_child_names)

        # Biological children: signing authority if not deceased
        elif any(t in rel for t in child_terms):
            entry["signing_authority"] = entry["status"] != "deceased"

        # Parents: only if no living spouse AND no living children
        elif any(t in rel for t in parent_terms):
            entry["signing_authority"] = not has_living_spouse and not has_living_children

        # Siblings: only if no living spouse, children, or parents
        elif any(t in rel for t in sibling_terms):
            entry["signing_authority"] = (
                not has_living_spouse and not has_living_children and not has_living_parents
            )

        # Everything else (friends, nieces/nephews, etc.): no signing authority
        elif any(t in rel for t in inlaw_terms):
            entry["signing_authority"] = False

        # Everything else (friends, nieces/nephews, etc.): no signing authority
        else:
            entry["signing_authority"] = False

    return ranked


def verify_heir_status(
    heir_name: str,
    city: str,
    api_key: str,
    depth: int = 0,
    max_depth: int = 2,
) -> dict:
    """Search for an obituary for a single heir to verify alive/dead.

    Returns dict: name, status, confidence, obituary_url, date_of_death,
                  sub_heirs (if deceased), search_log.
    """
    result = {
        "name": heir_name,
        "status": "unverified",
        "confidence": "low",
        "obituary_url": "",
        "date_of_death": "",
        "sub_heirs": [],
        "search_log": {"query": "", "result": "not_searched"},
    }

    results = _search_obituary(heir_name, city)
    result["search_log"]["query"] = f"{heir_name} obituary {city} Texas"

    if not results:
        # No search results at all — likely alive (no obituary exists online)
        result["status"] = "verified_living"
        result["confidence"] = "medium"
        result["search_log"]["result"] = "no_obituary_found"
        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
        return result

    # Try each result — fetch full page, then LLM parse
    for search_result in results:
        page_text = _fetch_page_text(search_result["url"])
        if page_text and len(page_text) >= 100:
            parsed = _parse_obituary_with_llm(
                obituary_text=page_text,
                owner_name=heir_name,
                city=city,
                address="",  # don't know heir's address
                api_key=api_key,
            )
            if parsed and parsed.get("confidence") in ("high", "medium"):
                result["status"] = "deceased"
                result["confidence"] = parsed.get("confidence", "medium")
                result["obituary_url"] = search_result["url"]
                result["date_of_death"] = parsed.get("date_of_death", "")
                result["search_log"]["result"] = "obituary_confirmed_deceased"
                if depth < max_depth:
                    result["sub_heirs"] = parsed.get("survivors", [])
                time.sleep(random.uniform(0.5, 1.0))
                return result
            time.sleep(random.uniform(0.5, 1.0))

    # Snippet fallback
    best_snippet = next((r for r in results if r.get("snippet")), None)
    if best_snippet:
        snippet_text = (
            f"Search result title: {best_snippet['title']}\n"
            f"URL: {best_snippet['url']}\n"
            f"Snippet: {best_snippet['snippet']}"
        )
        parsed = _parse_obituary_with_llm(
            obituary_text=snippet_text,
            owner_name=heir_name,
            city=city,
            address="",
            api_key=api_key,
        )
        if parsed and parsed.get("confidence") == "high":
            result["status"] = "deceased"
            result["confidence"] = "high"
            result["obituary_url"] = best_snippet["url"]
            result["date_of_death"] = parsed.get("date_of_death", "")
            result["search_log"]["result"] = "obituary_confirmed_deceased_snippet"
            return result

    # Searched but no obituary matched — likely alive
    result["status"] = "verified_living"
    result["confidence"] = "medium"
    result["search_log"]["result"] = "no_obituary_match"
    time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
    return result


def _heir_verify_priority(s: dict) -> int:
    """Sort key for the limited verification budget (lower = verify first).

    Spend the LLM-call budget on the closest heirs — the ones who actually hold
    signing authority under TX intestate succession (executor, spouse, children)
    — and let peripheral relatives (grandchildren, in-laws, friends) fall past
    the cap. They're still captured in Notes; we just don't chase them with a
    call. Mirrors the relationship tiers in rank_decision_makers().
    """
    rel = (s.get("relationship") or "").strip().lower()
    if "executor" in rel or "personal representative" in rel:
        return 0
    if ("in-law" not in rel and "in law" not in rel and "'s " not in rel
            and any(t in rel for t in ("wife", "husband", "spouse", "partner"))):
        return 1
    if ("in-law" not in rel and "step" not in rel
            and any(t in rel for t in ("son", "daughter", "child"))):
        return 2
    if "in-law" not in rel and any(t in rel for t in ("mother", "father", "parent")):
        return 3
    if "in-law" not in rel and any(t in rel for t in ("brother", "sister", "sibling")):
        return 4
    if any(t in rel for t in ("grandson", "granddaughter", "grandchild")):
        return 5
    return 6  # in-laws, step-children, friends, unknown relationship


def build_heir_map(
    parsed: dict,
    city: str,
    api_key: str,
    raw_name: str = "",
    max_depth: int = 2,
) -> tuple[list[dict], dict]:
    """Build heir map with verification, return (ranked_dms, error_info).

    For each survivor from the obituary, verifies alive/dead status.
    If a survivor is deceased and depth allows, recursively checks their heirs.
    Returns ranked decision-makers and an error/metadata dict.

    Verification is budgeted (MAX_HEIRS_VERIFIED / MAX_SUBHEIRS_VERIFIED): the
    closest heirs are verified first and any survivors beyond the cap are kept
    unverified in the returned map (so they still reach Notes) rather than each
    costing an LLM call — see the module-level caps for why.
    """
    # Never recurse deeper than the ceiling regardless of the caller's input,
    # so a stray max_heir_depth can't reintroduce the deep heirs-of-heirs fan-out.
    max_depth = min(max_depth, MAX_HEIR_DEPTH_CEILING)

    survivors = list(parsed.get("survivors", []))
    executor = parsed.get("executor_named", "")
    preceded = set(parsed.get("preceded_in_death", []))

    error_info: dict = {
        "heirs_verified_living": 0,
        "heirs_verified_deceased": 0,
        "heirs_unverified": 0,
        "missing_flags": [],
        "dm_confidence": "",
        "dm_confidence_reason": "",
    }

    if not survivors and not executor:
        # Second-pass: try aggressive extraction if raw obituary text is available
        raw_text = parsed.get("_raw_obituary_text", "")
        search_name = parsed.get("_search_name", raw_name)
        if raw_text and api_key:
            logger.info("  No survivors in first pass — trying aggressive extraction for %s", search_name)
            survivors = _extract_survivors_aggressive(raw_text, search_name, api_key)
            if survivors:
                error_info["missing_flags"].append("survivors_from_aggressive_pass")
            else:
                error_info["missing_flags"].append("no_survivors")
                return [], error_info
        else:
            error_info["missing_flags"].append("no_survivors")
            return [], error_info

    # Verify each survivor (parallelized for speed)
    heir_statuses: dict[str, str] = {}
    verified_names: set[str] = set()

    # Pre-filter: separate preceded_in_death from those needing verification
    to_verify: list[tuple[str, dict]] = []
    for s in survivors:
        name = s.get("name", "").strip()
        if not name or name in verified_names:
            continue
        verified_names.add(name)

        # Skip names known to be deceased from preceded_in_death
        if any(p.lower() in name.lower() or name.lower() in p.lower() for p in preceded):
            heir_statuses[name] = "deceased"
            error_info["heirs_verified_deceased"] += 1
            logger.debug("    Heir %s: preceded in death (skipped search)", name)
            continue

        to_verify.append((name, s))

    # Budget the depth-0 verification: verify the closest heirs first and leave
    # the rest unverified (but still in `survivors` → heir_map_json → Notes).
    if len(to_verify) > MAX_HEIRS_VERIFIED:
        to_verify.sort(key=lambda ns: _heir_verify_priority(ns[1]))
        over_cap = to_verify[MAX_HEIRS_VERIFIED:]
        to_verify = to_verify[:MAX_HEIRS_VERIFIED]
        for _n, _s in over_cap:
            _s["verification_skipped"] = True  # surfaced in Notes, no LLM call spent
        error_info["heirs_over_cap"] = len(over_cap)
        error_info["missing_flags"].append(
            f"heir_verification_capped_{len(over_cap)}_survivors_unverified"
        )
        logger.info(
            "  Heir verification capped at %d of %d survivors — %d peripheral "
            "survivor(s) captured unverified for Notes (no LLM call)",
            MAX_HEIRS_VERIFIED, len(to_verify) + len(over_cap), len(over_cap),
        )

    # Parallel depth-0 heir verification
    def _verify_depth0(args):
        vname, _ = args
        logger.info("    Verifying heir: %s", vname)
        return vname, verify_heir_status(
            heir_name=vname, city=city, api_key=api_key,
            depth=0, max_depth=max_depth,
        )

    sub_heirs_to_check: list[tuple[str, list]] = []
    if to_verify:
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            for vname, verification in pool.map(_verify_depth0, to_verify):
                heir_statuses[vname] = verification["status"]
                if verification["status"] == "deceased":
                    error_info["heirs_verified_deceased"] += 1
                    error_info["missing_flags"].append("heir_also_deceased")
                    if verification.get("sub_heirs"):
                        logger.info("    Heir %s is deceased, checking their survivors...", vname)
                        sub_heirs_to_check.append((vname, verification["sub_heirs"]))
                else:
                    error_info["heirs_unverified"] += 1

    # Parallel depth-1 sub-heir verification
    sub_verify_tasks: list[tuple[str, dict]] = []
    for _, sub_heirs in sub_heirs_to_check:
        for sub in sub_heirs:
            sub_name = sub.get("name", "").strip()
            if not sub_name or sub_name in verified_names:
                continue
            verified_names.add(sub_name)
            sub_verify_tasks.append((sub_name, sub))

    # Budget the depth-1 fan-out too: verify up to the cap, and record the rest
    # unverified so a deceased heir with a huge family still can't explode the
    # call count (the overflow sub-heirs still land in Notes via heir_map_json).
    if len(sub_verify_tasks) > MAX_SUBHEIRS_VERIFIED:
        over_subs = sub_verify_tasks[MAX_SUBHEIRS_VERIFIED:]
        sub_verify_tasks = sub_verify_tasks[:MAX_SUBHEIRS_VERIFIED]
        for sub_name, sub in over_subs:
            survivors.append({
                "name": sub_name,
                "relationship": sub.get("relationship", "grandchild"),
                "verification_skipped": True,
            })
        error_info["heirs_over_cap"] = error_info.get("heirs_over_cap", 0) + len(over_subs)
        error_info["missing_flags"].append(
            f"subheir_verification_capped_{len(over_subs)}_unverified"
        )

    def _verify_depth1(args):
        vname, _ = args
        logger.info("      Verifying sub-heir: %s", vname)
        return vname, _, verify_heir_status(
            heir_name=vname, city=city, api_key=api_key,
            depth=1, max_depth=max_depth,
        )

    if sub_verify_tasks:
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            for sub_name, sub, sub_v in pool.map(_verify_depth1, sub_verify_tasks):
                survivors.append({
                    "name": sub_name,
                    "relationship": sub.get("relationship", "grandchild"),
                })
                heir_statuses[sub_name] = sub_v["status"]
                if sub_v["status"] == "deceased":
                    error_info["heirs_verified_deceased"] += 1
                else:
                    error_info["heirs_unverified"] += 1

    # Rank decision-makers with verified statuses
    ranked = rank_decision_makers(survivors, executor, heir_statuses)

    # Set confidence
    living_dms = [r for r in ranked if r["status"] == "verified_living"]
    unverified_dms = [r for r in ranked if r["status"] == "unverified"]
    if living_dms:
        error_info["dm_confidence"] = "high"
        error_info["dm_confidence_reason"] = (
            f"DM verified living: {living_dms[0]['name']} ({living_dms[0]['relationship']})"
        )
    elif unverified_dms:
        error_info["dm_confidence"] = "medium"
        error_info["dm_confidence_reason"] = (
            f"{len(unverified_dms)} unverified heir(s), "
            f"top: {unverified_dms[0]['name']} ({unverified_dms[0]['relationship']})"
        )
    elif ranked:
        error_info["dm_confidence"] = "low"
        error_info["dm_confidence_reason"] = "all heirs confirmed deceased"
    else:
        error_info["dm_confidence"] = "low"
        error_info["dm_confidence_reason"] = "no usable heirs identified"

    return ranked, error_info


def _apply_obituary_match(
    notice: NoticeData,
    parsed: dict,
    url: str,
    source_type: str = "full_page",
    ranked_dms: list[dict] | None = None,
    error_info: dict | None = None,
) -> None:
    """Apply a confirmed obituary match to a notice record."""
    notice.owner_deceased = "yes"
    notice.date_of_death = parsed.get("date_of_death", "")
    notice.obituary_url = url
    notice.obituary_source_type = source_type

    if ranked_dms:
        # Deep prospecting: apply top 3 ranked decision-makers
        if len(ranked_dms) >= 1:
            dm = ranked_dms[0]
            notice.decision_maker_name = dm["name"]
            notice.decision_maker_relationship = dm["relationship"]
            notice.decision_maker_status = dm["status"]
            notice.decision_maker_source = dm.get("source", "obituary_survivors")
            # DM mailing address (populated by _lookup_dm_address)
            notice.decision_maker_street = dm.get("street", "")
            notice.decision_maker_city = dm.get("city", "")
            notice.decision_maker_state = dm.get("state", "")
            notice.decision_maker_zip = dm.get("zip", "")
        if len(ranked_dms) >= 2:
            dm = ranked_dms[1]
            notice.decision_maker_2_name = dm["name"]
            notice.decision_maker_2_relationship = dm["relationship"]
            notice.decision_maker_2_status = dm["status"]
        if len(ranked_dms) >= 3:
            dm = ranked_dms[2]
            notice.decision_maker_3_name = dm["name"]
            notice.decision_maker_3_relationship = dm["relationship"]
            notice.decision_maker_3_status = dm["status"]
        # Persist the full ranked heir list as JSON (all heirs, not just top 3)
        try:
            notice.heir_map_json = json.dumps(ranked_dms, ensure_ascii=False)
        except (TypeError, ValueError):
            notice.heir_map_json = ""
        # Signing chain summary: living heirs with signing authority
        signers = [
            e for e in ranked_dms
            if e.get("signing_authority") and e.get("status") != "deceased"
        ]
        notice.signing_chain_count = str(len(signers)) if signers else ""
        notice.signing_chain_names = ", ".join(e["name"] for e in signers) if signers else ""
    else:
        # Simple single-DM pick (Phase A only, no heir verification)
        survivors = parsed.get("survivors", [])
        executor = parsed.get("executor_named", "")
        if executor:
            notice.decision_maker_name = executor
            notice.decision_maker_relationship = "executor"
            notice.decision_maker_source = "obituary_survivors"
        elif survivors:
            dm_name, dm_rel = identify_decision_maker(survivors)
            notice.decision_maker_name = dm_name
            notice.decision_maker_relationship = dm_rel
            notice.decision_maker_source = "obituary_survivors"
        notice.decision_maker_status = "unverified"

    # Apply error map info
    if error_info:
        notice.heir_search_depth = str(error_info.get("heir_search_depth", "1"))
        living = error_info.get("heirs_verified_living", 0)
        deceased = error_info.get("heirs_verified_deceased", 0)
        unverified = error_info.get("heirs_unverified", 0)
        notice.heirs_verified_living = str(living) if living else ""
        notice.heirs_verified_deceased = str(deceased) if deceased else ""
        notice.heirs_unverified = str(unverified) if unverified else ""
        notice.dm_confidence = error_info.get("dm_confidence", "")
        notice.dm_confidence_reason = error_info.get("dm_confidence_reason", "")
        flags = error_info.get("missing_flags", [])
        notice.missing_data_flags = "|".join(flags) if flags else ""
    else:
        # No heir verification — set basic error map from source type
        notice.heir_search_depth = "0"
        flags = []
        if source_type == "snippet":
            flags.append("snippet_only")
        if not notice.decision_maker_name:
            flags.append("no_survivors")
            notice.dm_confidence = "low"
            notice.dm_confidence_reason = f"{source_type} match, no survivors extracted"
        else:
            notice.dm_confidence = "medium"
            notice.dm_confidence_reason = f"{source_type} match, DM unverified"
        notice.missing_data_flags = "|".join(flags)


def _process_one_candidate(
    notice: "NoticeData",
    raw_name: str,
    is_tax_name: bool,
    api_key: str,
    search_cache: dict,
    cache_lock: threading.Lock,
) -> dict:
    """Phase A worker: run obituary search + LLM parse for one owner name.

    Returns a result dict the main thread uses to update shared state.
    Mutates `notice.owner_deceased` only when a match is confirmed — each
    notice is owned by a single worker so no cross-thread write conflict.

    Result dict fields:
      status: 'confirmed' | 'cache_hit_dead' | 'cache_hit_alive' | 'no_match' | 'skipped'
      parsed, url, source_type: set when status in {'confirmed','cache_hit_dead'}
      raw_name, is_tax_name, notice: echoed for the caller's matches list
      search_name: last name actually searched (for logging)
      searched_queries: int — number of live DDG/Serper calls issued
      miss_reason: 'no_results' | 'fetch_failed' | 'llm_rejected' | None
    """
    result = {
        "notice": notice,
        "raw_name": raw_name,
        "is_tax_name": is_tax_name,
        "status": None,
        "parsed": None,
        "url": None,
        "source_type": None,
        "search_name": "",
        "searched_queries": 0,
        "miss_reason": None,
    }

    if is_tax_name:
        search_names = parse_tax_owner_name(raw_name)
    else:
        search_names = _parse_notice_owner_name(raw_name)
    # Probate "a/k/a" alias: try the decedent's alias as a fallback search name
    # so a decedent indexed only under their alias is still found.
    alias_names = []
    if notice.notice_type == "probate" and getattr(notice, "decedent_aka", ""):
        alias_names = _parse_notice_owner_name(notice.decedent_aka)
    if not search_names and not alias_names:
        result["status"] = "skipped"
        return result
    # Primary names first (cap 2), then the alias (cap 1) as a fallback — the
    # loop returns on the first match, so the alias is only used when the
    # primary name finds nothing.
    search_names = search_names[:2] + alias_names[:1]

    city = notice.city.strip() or "Austin"
    last_cache_key = ""
    last_results_empty = None
    last_any_fetch_succeeded = False

    for search_name in search_names:
        cache_key = search_name.lower().strip()
        last_cache_key = cache_key
        result["search_name"] = search_name

        with cache_lock:
            cached_present = cache_key in search_cache
            cached_val = search_cache.get(cache_key)

        if cached_present:
            if cached_val is not None:
                c_parsed, c_url, c_source = cached_val
                notice.owner_deceased = "yes"
                result["status"] = "cache_hit_dead"
                result["parsed"] = c_parsed
                result["url"] = c_url
                result["source_type"] = c_source
                return result
            else:
                result["status"] = "cache_hit_alive"
                return result

        results = _search_obituary(search_name, city)
        no_city_results = _search_obituary(search_name, "")
        seen_urls = {r["url"] for r in results}
        for r in no_city_results:
            if r["url"] not in seen_urls:
                results.append(r)
                seen_urls.add(r["url"])
        result["searched_queries"] += 1

        if not results:
            parts = search_name.split()
            if len(parts) == 3:
                name_no_mi = f"{parts[0]} {parts[2]}"
                results = _search_obituary(name_no_mi, city)

        if not results:
            parts = search_name.split()
            first = parts[0] if parts else ""
            last = parts[-1] if len(parts) >= 2 else ""
            if first and last:
                for variant in _get_name_variants(first):
                    nick_name = f"{variant} {last}".title()
                    results = _search_obituary(nick_name, city)
                    if results:
                        break

        if not results:
            results = _search_obituary(
                search_name, city,
                extra_terms='"death notice" OR "funeral"',
            )

        if not results:
            last_results_empty = True
            last_any_fetch_succeeded = False
            time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
            continue

        last_results_empty = False
        best_snippet_result = None
        any_fetch_succeeded = False
        parsed_match = None
        matched_url = None
        matched_source = None

        for r_item in results:
            page_text = _fetch_page_text(r_item["url"])
            if not page_text or len(page_text) < 100:
                if not best_snippet_result and r_item.get("snippet"):
                    best_snippet_result = r_item
                continue
            any_fetch_succeeded = True

            time.sleep(random.uniform(0.5, 1.0))

            parsed = _parse_obituary_with_llm(
                obituary_text=page_text,
                owner_name=search_name,
                city=city,
                address=notice.address,
                api_key=api_key,
            )

            if parsed and parsed.get("confidence") in ("high", "medium"):
                if not _dod_sanity_check(parsed.get("date_of_death", ""), notice):
                    continue
                _owner_ref = notice.tax_owner_name.strip() or notice.owner_name.strip() or search_name
                _ok, _why = _validate_obituary_match(parsed, notice, _owner_ref, "full_page", page_text)
                if not _ok:
                    logger.info("  Rejected full-page match for %s — %s", search_name, _why)
                    continue
                parsed["_raw_obituary_text"] = page_text
                parsed["_search_name"] = search_name
                parsed_match = parsed
                matched_url = r_item["url"]
                matched_source = "full_page"
                break

        last_any_fetch_succeeded = any_fetch_succeeded

        if parsed_match:
            notice.owner_deceased = "yes"
            with cache_lock:
                search_cache[cache_key] = (parsed_match, matched_url, matched_source)
            result["status"] = "confirmed"
            result["parsed"] = parsed_match
            result["url"] = matched_url
            result["source_type"] = matched_source
            return result

        if best_snippet_result:
            snippet_text = (
                f"Search result title: {best_snippet_result['title']}\n"
                f"URL: {best_snippet_result['url']}\n"
                f"Snippet: {best_snippet_result['snippet']}"
            )
            for r_item in results:
                if r_item["url"] != best_snippet_result["url"] and r_item.get("snippet"):
                    snippet_text += f"\n\nAdditional result: {r_item['title']}\nSnippet: {r_item['snippet']}"

            parsed = _parse_obituary_with_llm(
                obituary_text=snippet_text,
                owner_name=search_name,
                city=city,
                address=notice.address,
                api_key=api_key,
            )

            _conf = parsed.get("confidence", "") if parsed else ""
            # Snippet matches are lower-evidence than a fetched obituary page, so
            # only accept HIGH LLM confidence (was: medium also qualified). A
            # snippet at medium confidence is exactly where wrong-person matches
            # slip in — require a full-page fetch to confirm those instead.
            _snippet_ok = _conf == "high" and bool(
                parsed.get("full_name", "").strip()
                and parsed.get("date_of_death", "").strip()
            )
            if parsed and _snippet_ok:
                if not _dod_sanity_check(parsed.get("date_of_death", ""), notice):
                    parsed = None
                    _snippet_ok = False
            if parsed and _snippet_ok:
                _owner_ref = notice.tax_owner_name.strip() or notice.owner_name.strip() or search_name
                _ok, _why = _validate_obituary_match(parsed, notice, _owner_ref, "snippet", snippet_text)
                if not _ok:
                    logger.info("  Rejected snippet match for %s — %s", search_name, _why)
                    parsed = None
                    _snippet_ok = False
            if parsed and _snippet_ok:
                notice.owner_deceased = "yes"
                with cache_lock:
                    search_cache[cache_key] = (parsed, best_snippet_result["url"], "snippet")
                result["status"] = "confirmed"
                result["parsed"] = parsed
                result["url"] = best_snippet_result["url"]
                result["source_type"] = "snippet"
                return result

    # No match across all attempted search_names
    result["status"] = "no_match"
    if last_cache_key and result["searched_queries"] > 0:
        with cache_lock:
            if last_cache_key not in search_cache:
                search_cache[last_cache_key] = None

    if last_results_empty is True or last_results_empty is None:
        result["miss_reason"] = "no_results"
    elif not last_any_fetch_succeeded:
        result["miss_reason"] = "fetch_failed"
    else:
        result["miss_reason"] = "llm_rejected"

    time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
    return result


def enrich_obituary_data(
    notices: list[NoticeData],
    api_key: str,
    skip_heir_verification: bool = False,
    max_heir_depth: int = 2,
    skip_dm_address: bool = False,
    tracerfy_tier1: bool = False,
    skip_ancestry: bool = False,
    workers: int = 4,
    deadline: float | None = None,
) -> None:
    """Search for obituaries and enrich notices with deceased owner data.

    Two-phase pipeline:
      Phase A: Search for obituaries, confirm deceased owners.
      Phase B: For each confirmed deceased, verify heirs alive/dead,
               rank decision-makers, and apply joint-owner fallback
               for snippet-only matches without survivors.

    Updates notices in-place.
    """
    if not api_key:
        logger.warning("No Anthropic API key — skipping obituary enrichment")
        return

    # Wall-clock budget guard. When running under a bounded runtime (e.g. the
    # Apify 3600s actor timeout), `deadline` is an absolute time.monotonic()
    # value past which we stop *starting* new obituary work. Phase B (heir
    # verification + per-heir DM address lookups) is sequential and its runtime
    # scales with #deceased × #heirs — on heavy days it can run 30+ min and blow
    # the actor timeout, which aborts the whole run and loses the upload. With a
    # deadline we instead stop early and let the remaining records flow through
    # unenriched (they still upload; deceased flag/preset DMs are already set).
    _t0 = time.monotonic()

    def _deadline_reached() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    # Build candidate list: notices with owner names to search
    # Tuple: (notice, raw_name, is_tax_name)
    # is_tax_name=True → LAST FIRST MIDDLE format (use parse_tax_owner_name)
    # is_tax_name=False → FIRST LAST format from notice (use _parse_notice_owner_name)
    candidates = []
    for n in notices:
        # Skip records already enriched (e.g., from partial CSV re-import)
        if n.owner_deceased == "yes" and n.decision_maker_name:
            continue
        # Probate: search for decedent (the deceased), not executor/PR
        if n.notice_type == "probate" and n.decedent_name.strip():
            raw_name = n.decedent_name.strip()
            is_tax_name = False  # decedent_name is "First Last" format
        elif n.tax_owner_name.strip():
            raw_name = n.tax_owner_name.strip()
            is_tax_name = True
        elif n.owner_name.strip():
            raw_name = n.owner_name.strip()
            is_tax_name = False
        else:
            continue
        if _BUSINESS_RE.search(raw_name):
            continue
        # Probate notice is proof of death — set regardless of obituary search
        if n.notice_type == "probate" and n.decedent_name.strip():
            n.owner_deceased = "yes"
        candidates.append((n, raw_name, is_tax_name))

    if not candidates:
        logger.info("No notices with owner names for obituary search")
        return

    # ── Phase A: Obituary search ──────────────────────────────────────

    logger.info("Phase A: Searching obituaries for %d property owners...", len(candidates))

    # Search result cache: normalized_name → (parsed, url, source_type) or None (miss)
    # Prevents duplicate DDG + LLM calls when the same owner appears on multiple notices
    search_cache: dict[str, tuple[dict, str, str] | None] = {}
    cache_hits = 0

    confirmed = 0
    searched = 0
    skipped = 0
    miss_no_results = 0    # DDG returned 0 results even after fallbacks
    miss_fetch_failed = 0  # Got results but all page fetches returned < 100 chars
    miss_llm_rejected = 0  # Got page text but LLM returned no match / low confidence
    # Store match data for Phase B: [(notice, parsed, url, source_type, raw_name)]
    matches: list[tuple[NoticeData, dict, str, str, str, bool]] = []

    probate_preset_count = 0

    # First pass (sequential): probate presets require no API calls, and pulling
    # them out keeps the parallel section homogeneous (all do live searches).
    parallel_candidates: list[tuple["NoticeData", str, bool]] = []
    for notice, raw_name, is_tax_name in candidates:
        if (
            notice.notice_type == "probate"
            and notice.owner_name
            and notice.decedent_name
            and not _is_same_person(notice.owner_name, notice.decedent_name)
        ):
            synthetic_parsed = {
                "confidence": "high",
                "full_name": notice.decedent_name or notice.owner_name,
                "date_of_death": "",
                "survivors": [],
                "executor_named": notice.owner_name,
                "_probate_preset": True,
            }
            matches.append((notice, synthetic_parsed, notice.source_url or "", "probate_preset", raw_name, is_tax_name))
            confirmed += 1
            probate_preset_count += 1
            logger.info(
                "  Probate preset: survivor=%s, decedent=%s",
                notice.owner_name, notice.decedent_name,
            )
            continue
        parallel_candidates.append((notice, raw_name, is_tax_name))

    # Parallel pass: live obituary search + LLM parse.
    # Thread-safe cache + counter locks guard shared state.
    cache_lock = threading.Lock()
    counter_lock = threading.Lock()
    total_parallel = len(parallel_candidates)
    logger.info(
        "Phase A parallel: %d candidates across %d workers (%d probate presets handled sequentially)",
        total_parallel, workers, probate_preset_count,
    )

    if parallel_candidates and _deadline_reached():
        logger.warning(
            "Obituary time budget exhausted before Phase A — skipping %d live "
            "obituary searches (probate presets already handled)",
            len(parallel_candidates),
        )
        parallel_candidates = []

    if parallel_candidates:
        processed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _process_one_candidate,
                    n, raw_name, is_tax_name,
                    api_key, search_cache, cache_lock,
                )
                for n, raw_name, is_tax_name in parallel_candidates
            ]
            for future in as_completed(futures):
                result = future.result()
                status = result["status"]
                search_name = result["search_name"] or result["raw_name"]

                with counter_lock:
                    processed += 1
                    searched += result["searched_queries"]

                    if status == "confirmed":
                        matches.append((
                            result["notice"], result["parsed"], result["url"],
                            result["source_type"], result["raw_name"], result["is_tax_name"],
                        ))
                        confirmed += 1
                        logger.info(
                            "  [%d/%d] %s: DECEASED (%s, DOD: %s)",
                            processed, total_parallel, search_name,
                            result["source_type"],
                            (result["parsed"] or {}).get("date_of_death", "unknown"),
                        )
                    elif status == "cache_hit_dead":
                        matches.append((
                            result["notice"], result["parsed"], result["url"],
                            result["source_type"], result["raw_name"], result["is_tax_name"],
                        ))
                        confirmed += 1
                        cache_hits += 1
                        logger.debug(
                            "  [%d/%d] %s: cache hit (deceased)",
                            processed, total_parallel, search_name,
                        )
                    elif status == "cache_hit_alive":
                        cache_hits += 1
                        logger.debug(
                            "  [%d/%d] %s: cache hit (no match)",
                            processed, total_parallel, search_name,
                        )
                    elif status == "skipped":
                        skipped += 1
                    elif status == "no_match":
                        reason = result["miss_reason"]
                        if reason == "no_results":
                            miss_no_results += 1
                        elif reason == "fetch_failed":
                            miss_fetch_failed += 1
                        elif reason == "llm_rejected":
                            miss_llm_rejected += 1
                        logger.debug(
                            "  [%d/%d] %s: no obituary match (%s)",
                            processed, total_parallel, search_name, reason,
                        )

                    if processed % 25 == 0:
                        logger.info(
                            "Phase A progress: %d/%d (confirmed=%d, cache_hits=%d)",
                            processed, total_parallel, confirmed, cache_hits,
                        )

    logger.info(
        "Phase A complete: %d confirmed deceased (%d probate preset), %d searches, %d skipped, %d cache hits | "
        "no-match: no_results=%d, fetch_failed=%d, llm_rejected=%d",
        confirmed, probate_preset_count, searched, skipped, cache_hits,
        miss_no_results, miss_fetch_failed, miss_llm_rejected,
    )

    # ── Phase A.5: Ancestry fallback for unresolved candidates ─────────
    # Only search Ancestry for candidates that Phase A couldn't confirm.
    # Saves page loads (100/day limit) and time (~15s/lookup).

    import config as cfg  # noqa: PLC0415
    if _deadline_reached():
        logger.warning(
            "Obituary time budget reached — skipping Ancestry fallback"
        )
    elif not skip_ancestry and cfg.ANCESTRY_EMAIL and cfg.ANCESTRY_PASSWORD:
        import asyncio
        import ancestry_enricher

        unresolved = [
            (idx, notice, raw_name, is_tax_name)
            for idx, (notice, raw_name, is_tax_name) in enumerate(candidates)
            if notice.owner_deceased != "yes"
            and not (notice.notice_type == "probate" and notice.owner_name and notice.owner_street)
        ]

        if unresolved:
            logger.info("Ancestry fallback: %d unresolved candidates (of %d total)",
                        len(unresolved), len(candidates))
            ancestry_match_data = []  # collect (notice, raw_name, is_tax_name, result) for Phase B

            async def _ancestry_fallback():
                ancestry_hits = 0
                pw, context, page = await ancestry_enricher.launch_browser()
                if not page:
                    logger.warning(
                        "Ancestry: no authenticated session — skipping fallback. "
                        "Seed one with: python src/ancestry_enricher.py bootstrap"
                    )
                    return ancestry_hits

                try:
                    for idx, notice, raw_name, is_tax_name in unresolved:
                        if ancestry_enricher.is_circuit_broken() or not ancestry_enricher._can_load_page():
                            logger.warning("Ancestry: circuit breaker or daily limit — stopping fallback")
                            break

                        if is_tax_name:
                            names = parse_tax_owner_name(raw_name)
                        else:
                            names = _parse_notice_owner_name(raw_name)
                        if not names:
                            continue

                        search_name = names[0]
                        city = notice.city.strip() or "Austin"

                        result = await ancestry_enricher.lookup_deceased(
                            page, name=search_name, city=city, state="TX"
                        )
                        if result and result.get("confirmed_deceased"):
                            notice.owner_deceased = "yes"
                            notice.date_of_death = result.get("date_of_death", "")
                            notice.obituary_url = result.get("source_url", "")
                            notice.obituary_source_type = result.get("source_type", "ancestry")
                            ancestry_match_data.append((notice, raw_name, is_tax_name, result))
                            ancestry_hits += 1
                            logger.info(
                                "  Ancestry hit [%d/%d]: %s → %s (source: %s)",
                                ancestry_hits, len(unresolved), search_name,
                                result.get("full_name", ""), result.get("source_type", ""),
                            )
                finally:
                    await ancestry_enricher.close_browser(pw, context)

                return ancestry_hits

            try:
                ancestry_hits = asyncio.run(_ancestry_fallback())
                if ancestry_hits:
                    confirmed += ancestry_hits
                    # Enrich Ancestry hits with DuckDuckGo obituary text for heir extraction
                    for notice, raw_name, is_tax_name, result in ancestry_match_data:
                        confirmed_name = result.get("full_name", "")
                        city = notice.city.strip() or "Austin"
                        source_url = result.get("source_url", "")
                        source_type = "ancestry"

                        # Try DuckDuckGo search using the Ancestry-confirmed name
                        ancestry_parsed = None
                        if confirmed_name:
                            obit_results = _search_obituary(confirmed_name, city)
                            if obit_results:
                                for obit_r in obit_results[:3]:
                                    page_text = _fetch_page_text(obit_r["url"])
                                    if page_text and len(page_text) > 200:
                                        parsed = _parse_obituary_with_llm(
                                            obituary_text=page_text,
                                            owner_name=confirmed_name,
                                            city=city,
                                            address=notice.address,
                                            api_key=api_key,
                                        )
                                        if parsed and parsed.get("confidence") in ("high", "medium"):
                                            parsed["_raw_obituary_text"] = page_text
                                            parsed["_search_name"] = confirmed_name
                                            # Preserve Ancestry death confirmation
                                            if not parsed.get("date_of_death"):
                                                parsed["date_of_death"] = result.get("date_of_death", "")
                                            ancestry_parsed = parsed
                                            source_url = obit_r["url"]
                                            source_type = "ancestry+full_page"
                                            notice.obituary_url = source_url
                                            logger.info(
                                                "  Ancestry enriched: %s → full obituary from %s",
                                                confirmed_name, obit_r["url"],
                                            )
                                            break

                        # Fallback: minimal Ancestry-only parsed dict
                        if not ancestry_parsed:
                            ancestry_parsed = {
                                "confidence": "high",
                                "date_of_death": result.get("date_of_death", ""),
                                "deceased_name": confirmed_name,
                                "survivors": [],
                                "executor_named": "",
                            }
                            # If Ancestry returned family members, add them as survivors
                            for fm in result.get("family_members", []):
                                ancestry_parsed["survivors"].append({
                                    "name": fm.get("name", ""),
                                    "relationship": fm.get("relationship", ""),
                                })

                        matches.append((notice, ancestry_parsed, source_url,
                                        source_type, raw_name, is_tax_name))
                    logger.info("Ancestry fallback: resolved %d/%d unresolved candidates",
                                ancestry_hits, len(unresolved))
            except Exception as e:
                logger.warning("Ancestry fallback failed: %s", e)
    elif not skip_ancestry:
        logger.debug("Ancestry: no credentials configured — skipping")

    # ── Phase B: Heir verification + ranked DMs + joint-owner fallback ─

    full_page_matches = sum(1 for _, _, _, st, *_ in matches if st == "full_page")
    snippet_matches = sum(1 for _, _, _, st, *_ in matches if st == "snippet")
    logger.info(
        "Phase B: Processing %d deceased records (%d full-page, %d snippet)...",
        len(matches), full_page_matches, snippet_matches,
    )

    heir_verified_count = 0
    joint_owner_dm_count = 0
    research_dm_count = 0
    snippet_dm_count = 0
    no_dm_possible_count = 0
    estate_fallback_count = 0
    dm_addr_sources = {"tx_cad": 0, "people_search": 0,
                       "ddg_people_search": 0, "inline_tracerfy": 0, "batch_tracerfy": 0}

    # Phase B is per-record independent — each iteration mutates only its own
    # notice, and every shared primitive it calls is thread-safe (llm_client is
    # already run concurrently in Phase A; DDGS via _ddgs_lock; Firecrawl budget
    # via _firecrawl_lock; Serper via requests). So process records concurrently
    # like Phase A instead of one-at-a-time. The worker's counters are
    # function-local (same names the body already increments) and summed after
    # the pool; a record past the deadline returns ran=False and uploads
    # unenriched (graceful degradation preserved, now across the whole batch).
    def _pb_worker(j, item):
        notice, parsed, url, source_type, raw_name, is_tax_name = item
        heir_verified_count = 0
        joint_owner_dm_count = 0
        research_dm_count = 0
        snippet_dm_count = 0
        estate_fallback_count = 0
        dm_addr_sources = {}
        if _deadline_reached():
            return (False, 0, 0, 0, 0, 0, {})
        city = notice.city.strip() or "Austin"
        survivors = parsed.get("survivors", [])
        has_survivors = bool(survivors) or bool(parsed.get("executor_named", ""))

        ranked_dms = None
        error_info = None

        # Path -1: Joint co-owner takes priority as DM
        # When property has 2+ owners and one is confirmed deceased,
        # the surviving co-owner is the most actionable contact (on title, likely spouse)
        if not parsed.get("_probate_preset"):
            # Try tax name format first, then notice owner_name
            co_owner_names = (
                parse_tax_owner_name(raw_name) if is_tax_name
                else _parse_notice_owner_name(raw_name)
            )
            # Also check notice.owner_name for "X And Y" not captured above
            if len(co_owner_names) < 2 and notice.owner_name.strip():
                co_owner_names = _parse_notice_owner_name(notice.owner_name.strip())

            if len(co_owner_names) >= 2:
                # Deceased is first name (matched in Phase A); co-owner is second
                co_owner_name = co_owner_names[1]
                co_owner_status = "unverified"

                if not skip_heir_verification:
                    logger.info(
                        "  [%d/%d] Joint co-owner on title: %s — verifying alive...",
                        j, len(matches), co_owner_name,
                    )
                    verification = verify_heir_status(
                        heir_name=co_owner_name, city=city, api_key=api_key,
                    )
                    co_owner_status = verification["status"]

                if co_owner_status != "deceased":
                    ranked_dms = [{
                        "name": co_owner_name,
                        "relationship": "spouse",
                        "status": co_owner_status,
                        "source": "joint_owner_on_title",
                        "rank": 1,
                    }]
                    _co_conf = "high" if co_owner_status == "verified_living" else "medium"
                    error_info = {
                        "heir_search_depth": 1 if not skip_heir_verification else 0,
                        "heirs_verified_living": 1 if co_owner_status == "verified_living" else 0,
                        "heirs_verified_deceased": 0,
                        "heirs_unverified": 1 if co_owner_status == "unverified" else 0,
                        "missing_flags": [],
                        "dm_confidence": _co_conf,
                        "dm_confidence_reason": f"joint co-owner on title ({co_owner_status})",
                    }
                    joint_owner_dm_count += 1
                    logger.info(
                        "  [%d/%d] DM = co-owner %s (joint_owner_on_title, %s)",
                        j, len(matches), co_owner_name, _co_conf,
                    )
                else:
                    logger.info(
                        "  [%d/%d] Co-owner %s is also deceased — falling through to heir search",
                        j, len(matches), co_owner_name,
                    )

        # Path 0: Probate preset — CAD owner-of-record is the DM, address from notice
        if parsed.get("_probate_preset"):
            _dm_rel, _dm_reason = _probate_dm_relationship(notice)
            ranked_dms = [{
                "name": notice.owner_name,
                "relationship": _dm_rel,
                "status": "verified_living",
                "source": "probate_notice",
                "rank": 1,
                "street": notice.owner_street,
                "city": notice.owner_city or "Austin",
                "state": "TX",
                "zip": notice.owner_zip,
            }]
            error_info = {
                "heir_search_depth": 0,
                "heirs_verified_living": 1,
                "heirs_verified_deceased": 0,
                "heirs_unverified": 0,
                "dm_confidence": "high",
                "dm_confidence_reason": _dm_reason,
            }
            # Mark decedent as deceased
            if notice.decedent_name:
                notice.owner_deceased = "yes"
            logger.info(
                "  [%d/%d] Probate preset DM: %s (%s) at %s",
                j, len(matches), notice.owner_name, _dm_rel, notice.owner_street,
            )

        # Path 1: Full-page match with survivors → run heir verification
        # Skip if Path -1 (joint co-owner) or Path 0 (probate) already set DM
        if has_survivors and not skip_heir_verification and not ranked_dms:
            logger.info(
                "  [%d/%d] Verifying heirs for %s (%d survivors)...",
                j, len(matches), notice.owner_name, len(survivors),
            )
            ranked_dms, error_info = build_heir_map(
                parsed=parsed,
                city=city,
                api_key=api_key,
                raw_name=raw_name,
                max_depth=max_heir_depth,
            )
            error_info["heir_search_depth"] = 1
            heir_verified_count += 1

        # Path 2: No survivors + listing URL → re-search for specific obituary
        if not has_survivors and not ranked_dms and source_type == "snippet" and _is_listing_url(url):
            search_names = parse_tax_owner_name(raw_name)
            if search_names:
                logger.info(
                    "  [%d/%d] Re-searching for specific obituary (listing URL): %s",
                    j, len(matches), search_names[0],
                )
                new_parsed, new_url, new_source = _refetch_specific_obituary(
                    name=search_names[0],
                    city=city,
                    original_url=url,
                    api_key=api_key,
                    address=notice.address,
                )
                if new_parsed:
                    parsed = new_parsed
                    url = new_url
                    source_type = new_source
                    survivors = parsed.get("survivors", [])
                    has_survivors = bool(survivors) or bool(parsed.get("executor_named", ""))
                    if has_survivors and not skip_heir_verification:
                        ranked_dms, error_info = build_heir_map(
                            parsed=parsed,
                            city=city,
                            api_key=api_key,
                            raw_name=raw_name,
                            max_depth=max_heir_depth,
                        )
                        error_info["heir_search_depth"] = 1
                        research_dm_count += 1

        # Path 3a: No survivors + snippet → try Firecrawl on the snippet URL
        #   The URL was likely 403-blocked during Phase A; Firecrawl may bypass it.
        if not has_survivors and not ranked_dms and source_type == "snippet" and url:
            search_names = parse_tax_owner_name(raw_name)
            search_name = search_names[0] if search_names else raw_name
            logger.info(
                "  [%d/%d] Trying Firecrawl on snippet URL for %s: %s",
                j, len(matches), search_name, url,
            )
            fc_text = _fetch_firecrawl(url, priority="medium")
            if fc_text and len(fc_text) >= 200:
                fc_parsed = _parse_obituary_with_llm(
                    obituary_text=fc_text,
                    owner_name=search_name,
                    city=city,
                    address=notice.address,
                    api_key=api_key,
                )
                if fc_parsed:
                    parsed = fc_parsed
                    parsed["_raw_obituary_text"] = fc_text
                    source_type = "full_page"
                    survivors = parsed.get("survivors", [])
                    has_survivors = bool(survivors) or bool(parsed.get("executor_named", ""))
                    if has_survivors:
                        logger.info(
                            "  [%d/%d] Firecrawl upgraded snippet → full page (%d survivors)",
                            j, len(matches), len(survivors),
                        )
                        if not skip_heir_verification:
                            ranked_dms, error_info = build_heir_map(
                                parsed=parsed,
                                city=city,
                                api_key=api_key,
                                raw_name=raw_name,
                                max_depth=max_heir_depth,
                            )
                            error_info["heir_search_depth"] = 1
                            error_info["missing_flags"] = error_info.get("missing_flags", [])
                            error_info["missing_flags"].append("dm_from_firecrawl_upgrade")
                            research_dm_count += 1

        # Path 3b: No survivors + snippet → targeted survivor search
        if not has_survivors and not ranked_dms and source_type == "snippet":
            search_names = parse_tax_owner_name(raw_name)
            if search_names:
                logger.info(
                    "  [%d/%d] Targeted snippet survivor search: %s",
                    j, len(matches), search_names[0],
                )
                extra_survivors = _search_survivors_targeted(
                    name=search_names[0],
                    city=city,
                    api_key=api_key,
                )
                if extra_survivors:
                    parsed["survivors"] = extra_survivors
                    survivors = extra_survivors
                    has_survivors = True
                    if not skip_heir_verification:
                        ranked_dms, error_info = build_heir_map(
                            parsed=parsed,
                            city=city,
                            api_key=api_key,
                            raw_name=raw_name,
                            max_depth=max_heir_depth,
                        )
                        error_info["heir_search_depth"] = 1
                        error_info["missing_flags"] = error_info.get("missing_flags", [])
                        error_info["missing_flags"].append("dm_from_targeted_snippet")
                        snippet_dm_count += 1

        # Path 4: No survivors — try joint owner from tax record as probable spouse
        if not has_survivors and not ranked_dms:
            search_names = parse_tax_owner_name(raw_name)
            if len(search_names) >= 2:
                # Second name = probable spouse (e.g., "WILLIAMS DANIEL H & CHRISTINE C")
                spouse_name = search_names[1]
                spouse_status = "unverified"

                # Optionally verify spouse is alive
                if not skip_heir_verification:
                    logger.info(
                        "  [%d/%d] No survivors in obituary, checking joint owner: %s",
                        j, len(matches), spouse_name,
                    )
                    verification = verify_heir_status(
                        heir_name=spouse_name,
                        city=city,
                        api_key=api_key,
                    )
                    spouse_status = verification["status"]

                ranked_dms = [{
                    "name": spouse_name,
                    "relationship": "spouse",
                    "status": spouse_status,
                    "source": "tax_record_joint_owner",
                    "rank": 1,
                }]
                _jt_conf = "high" if spouse_status == "verified_living" else (
                    "medium" if spouse_status != "deceased" else "low"
                )
                error_info = {
                    "heir_search_depth": 1 if not skip_heir_verification else 0,
                    "heirs_verified_living": 1 if spouse_status == "verified_living" else 0,
                    "heirs_verified_deceased": 1 if spouse_status == "deceased" else 0,
                    "heirs_unverified": 1 if spouse_status == "unverified" else 0,
                    "missing_flags": ["dm_from_tax_record"],
                    "dm_confidence": _jt_conf,
                    "dm_confidence_reason": f"joint owner from tax record ({spouse_status})",
                }
                if source_type == "snippet":
                    error_info["missing_flags"].append("snippet_only")
                joint_owner_dm_count += 1

        # Path 4b: No survivors — try joint owner from notice text (owner_name)
        #   Catches cases where tax record is single-name but notice text has "X and Y"
        if not has_survivors and not ranked_dms and notice.owner_name.strip():
            owner = notice.owner_name.strip()
            # Split on " & " or " and " (case-insensitive)
            joint_parts = re.split(r"\s+(?:&|and)\s+", owner, flags=re.IGNORECASE)
            if len(joint_parts) >= 2:
                # Deceased is likely first name; co-owner is second
                deceased_name = joint_parts[0].strip()
                co_owner = joint_parts[1].strip()
                # If co-owner is just a first name, append the deceased's last name
                if co_owner and " " not in co_owner:
                    deceased_tokens = deceased_name.split()
                    if len(deceased_tokens) >= 2:
                        co_owner = f"{co_owner} {deceased_tokens[-1]}"
                if co_owner and len(co_owner.split()) >= 2:
                    spouse_status = "unverified"
                    if not skip_heir_verification:
                        logger.info(
                            "  [%d/%d] No survivors, checking notice co-owner: %s",
                            j, len(matches), co_owner,
                        )
                        verification = verify_heir_status(
                            heir_name=co_owner,
                            city=city,
                            api_key=api_key,
                        )
                        spouse_status = verification["status"]
                    ranked_dms = [{
                        "name": co_owner,
                        "relationship": "spouse",
                        "status": spouse_status,
                        "source": "notice_text_joint_owner",
                        "rank": 1,
                    }]
                    _nt_conf = "high" if spouse_status == "verified_living" else (
                        "medium" if spouse_status != "deceased" else "low"
                    )
                    error_info = {
                        "heir_search_depth": 1 if not skip_heir_verification else 0,
                        "heirs_verified_living": 1 if spouse_status == "verified_living" else 0,
                        "heirs_verified_deceased": 1 if spouse_status == "deceased" else 0,
                        "heirs_unverified": 1 if spouse_status == "unverified" else 0,
                        "missing_flags": ["dm_from_notice_text"],
                        "dm_confidence": _nt_conf,
                        "dm_confidence_reason": f"joint owner from notice text ({spouse_status})",
                    }
                    if source_type == "snippet":
                        error_info["missing_flags"].append("snippet_only")
                    joint_owner_dm_count += 1

        # Path 5: Estate-of fallback — no survivors, mail to property address
        if not has_survivors and not ranked_dms:
            # Resolve the decedent's name from the best source. code_violation
            # records leave owner_name blank (the real name is in tax_owner_name,
            # NAMELF) — without this the estate comes out nameless "Estate of ".
            _estate_person = _estate_display_name(notice)
            estate_name = f"Estate of {_estate_person}" if _estate_person else "Estate of "
            ranked_dms = [{
                "name": estate_name,
                "relationship": "estate",
                "status": "estate_fallback",
                "source": "estate_fallback",
                "rank": 1,
                "street": notice.address,
                "city": notice.city or "Austin",
                "state": "TX",
                "zip": notice.zip,
            }]
            error_info = {
                "heir_search_depth": 0,
                "heirs_verified_living": 0,
                "heirs_verified_deceased": 0,
                "heirs_unverified": 0,
                "missing_flags": ["no_survivors", "dm_from_estate_fallback"],
                "dm_confidence": "low",
                "dm_confidence_reason": "no living relative found; mailing to property address",
            }
            estate_fallback_count += 1
            logger.info(
                "  [%d/%d] %s: estate fallback -> %s at %s",
                j, len(matches), notice.owner_name, estate_name, notice.address,
            )

        # ── Address Lookup for ALL signing-authority heirs ──────────
        # Look up mailing addresses for every heir in the signing chain,
        # not just DM #1. This enables multi-heir skip tracing for deal closing.
        import config as cfg  # noqa: PLC0415
        if not skip_dm_address and ranked_dms:
            # Identify which heirs need address lookup
            signing_heirs = [
                dm for dm in ranked_dms
                if dm.get("signing_authority")
                and dm.get("status") != "deceased"
                and dm.get("name")
                and not dm.get("street")
            ]
            # Always include DM #1 even if not signing authority (primary contact)
            if ranked_dms and not ranked_dms[0].get("street") and ranked_dms[0] not in signing_heirs:
                signing_heirs.insert(0, ranked_dms[0])

            for dm in signing_heirs:
                if _deadline_reached():
                    logger.warning(
                        "  [%d/%d] Time budget reached mid-record — stopping "
                        "heir address lookups (applying what's collected)",
                        j, len(matches),
                    )
                    break
                dm_name = dm["name"]
                # Get DM's city from survivor data if available
                dm_city_hint = ""
                for s in parsed.get("survivors", []):
                    if s.get("name", "").lower() == dm_name.lower():
                        dm_city_hint = s.get("city", "")
                        break
                if not dm_city_hint:
                    dm_city_hint = city  # fall back to property city

                logger.info(
                    "  [%d/%d] Looking up address for DM: %s (city hint: %s)",
                    j, len(matches), dm_name, dm_city_hint or "unknown",
                )
                addr = _lookup_dm_address(dm_name, dm_city_hint, api_key,
                                          tracerfy_tier1=tracerfy_tier1)
                if addr.get("street"):
                    dm.update(addr)
                    source = addr.get("source", "unknown")
                    if source in dm_addr_sources:
                        dm_addr_sources[source] += 1
                    else:
                        dm_addr_sources[source] = 1
                    logger.info(
                        "  [%d/%d] Found DM address (%s): %s, %s %s %s",
                        j, len(matches), source, addr["street"], addr["city"],
                        addr["state"], addr["zip"],
                    )
                    continue

                # Tier 3 inline: Tracerfy when all preceding tiers missed
                if cfg.TRACERFY_API_KEY:
                    tracerfy_result = _lookup_dm_address_tracerfy(
                        dm_name, dm_city_hint or city,
                        address=notice.address, zip_code=notice.zip,
                    )
                    if tracerfy_result and tracerfy_result.get("street"):
                        dm.update(tracerfy_result)
                        dm_addr_sources["inline_tracerfy"] = (
                            dm_addr_sources.get("inline_tracerfy", 0) + 1
                        )
                        logger.info(
                            "  [%d/%d] Inline Tracerfy found address for %s: %s",
                            j, len(matches), dm_name, tracerfy_result["street"],
                        )
                        continue

                # Tier 4: Property address fallback (DM #1 only — others left empty)
                if dm is ranked_dms[0] and dm.get("source") != "estate_fallback":
                    dm["street"] = notice.address
                    dm["city"] = notice.city or "Austin"
                    dm["state"] = "TX"
                    dm["zip"] = notice.zip
                    dm_addr_sources["property_fallback"] = (
                        dm_addr_sources.get("property_fallback", 0) + 1
                    )
                    logger.info(
                        "  [%d/%d] Using property address as DM fallback: %s",
                        j, len(matches), notice.address,
                    )

        # Apply the match with all collected data
        _apply_obituary_match(
            notice=notice,
            parsed=parsed,
            url=url,
            source_type=source_type,
            ranked_dms=ranked_dms,
            error_info=error_info,
        )

        return (True, heir_verified_count, joint_owner_dm_count,
                research_dm_count, snippet_dm_count, estate_fallback_count,
                dm_addr_sources)

    # Run Phase B records concurrently, then fold the per-record stats together.
    pb_skipped = 0
    pb_done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pb_futures = [
            pool.submit(_pb_worker, j, item)
            for j, item in enumerate(matches, 1)
        ]
        for fut in as_completed(pb_futures):
            ran, hv, jo, rd, sd, ef, addr = fut.result()
            heir_verified_count += hv
            joint_owner_dm_count += jo
            research_dm_count += rd
            snippet_dm_count += sd
            estate_fallback_count += ef
            for k, v in addr.items():
                dm_addr_sources[k] = dm_addr_sources.get(k, 0) + v
            pb_done += 1
            if not ran:
                pb_skipped += 1
            elif pb_done % 10 == 0:
                logger.info(
                    "Phase B progress: %d/%d (heir-verified=%d, joint-owner-DM=%d)",
                    pb_done, len(matches), heir_verified_count, joint_owner_dm_count,
                )
    if pb_skipped:
        logger.warning(
            "Obituary time budget reached after %.0fs — %d/%d Phase B records "
            "skipped, uploaded without heir/DM enrichment (raise "
            "obituary_deadline_secs or obituary_workers to enrich more).",
            time.monotonic() - _t0, pb_skipped, len(matches),
        )

    # ── Phase C: Batch Tracerfy for remaining DMs without addresses ──
    import config as cfg
    if not skip_dm_address and cfg.TRACERFY_API_KEY and not _deadline_reached():
        dm_needs_addr = [
            n for n in notices
            if n.decision_maker_name and not n.decision_maker_street
        ]
        if dm_needs_addr:
            logger.info(
                "Phase C: Batch Tracerfy skip trace for %d DMs without addresses...",
                len(dm_needs_addr),
            )
            before_count = sum(1 for n in dm_needs_addr if n.decision_maker_street)
            _batch_tracerfy_lookup(dm_needs_addr)
            batch_found = sum(1 for n in dm_needs_addr if n.decision_maker_street) - before_count
            dm_addr_sources["batch_tracerfy"] += batch_found

    # ── Summary ───────────────────────────────────────────────────────

    dm_identified = sum(1 for n in notices if n.decision_maker_name)
    dm_verified_living = sum(1 for n in notices if n.decision_maker_status == "verified_living")
    dm_with_addr = sum(1 for n in notices if n.decision_maker_street)

    logger.info("Obituary enrichment complete:")
    logger.info("  Confirmed deceased:     %d", confirmed)
    logger.info("  DM identified:          %d/%d (%.0f%%)",
                dm_identified, confirmed, 100 * dm_identified / confirmed if confirmed else 0)
    logger.info("  DM verified living:     %d", dm_verified_living)
    logger.info("  Heir-verified records:  %d", heir_verified_count)
    logger.info("  Joint-owner DM added:   %d", joint_owner_dm_count)
    logger.info("  Re-search DM added:     %d", research_dm_count)
    logger.info("  Snippet DM added:       %d", snippet_dm_count)
    logger.info("  Estate-of fallback:     %d", estate_fallback_count)
    logger.info("  No DM possible:         %d", no_dm_possible_count)
    if dm_identified:
        logger.info("  DM addresses found:     %d/%d (%.0f%%)",
                    dm_with_addr, dm_identified,
                    100 * dm_with_addr / dm_identified)
        active_sources = {k: v for k, v in dm_addr_sources.items() if v > 0}
        if active_sources:
            logger.info("  DM address sources:")
            for source, count in active_sources.items():
                logger.info("    %-20s %d", source, count)

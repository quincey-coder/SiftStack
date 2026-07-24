"""Relevance filter for code-enforcement leads.

Code-enforcement data is only useful to an investor as OPEN cases that signal
NEGLECT — an absentee, overwhelmed, or motivated owner. Two gates, applied to
code_violation NoticeData before they reach DataSift:

  1. Drop CLOSED cases — closed / complied / "in compliance" means the problem
     was already fixed, so it's not a lead.
  2. Keep only NEGLECT/DISTRESS violation types — tall grass, uncut lawn, junk,
     debris, dilapidated / condemned / fire-damaged structures, etc. — and drop
     administrative ones (permits, signs, zoning, noise...). Obvious types resolve
     by keyword; everything else is classified by the LLM (distinct types deduped
     into a single batched call, cached per run).

Shared by the Travis Austin-Code scraper and the Bell/Williamson open-records
ingest so both apply identical criteria.
"""
import logging

import config
import llm_client
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# Status substrings that mean the case is resolved/closed (case-insensitive).
_CLOSED_STATUS_MARKERS = (
    "closed", "complied", "compliance met", "in compliance", "resolved",
    "void", "cancel", "complete", "inactive", "no violation", "dismiss",
    "abated", "withdrawn", "duplicate", "expired",
)

# High-confidence NEGLECT signals — auto-keep, no LLM call needed.
_NEGLECT_KEYWORDS = (
    "grass", "weed", "lawn", "overgrow", "vegetation", "brush", "mowing",
    "debris", "rubbish", "trash", "litter", "garbage", "refuse", "dumping",
    "junk", "inoperable", "junked", "abandoned vehicle",
    "dilapidat", "substandard", "dangerous", "unsafe", "derelict",
    "condemn", "structure condition", "abatement", "nuisance",
    "accumulation", "fire damage", "boarded", "vacant", "graffiti",
    "unsanitary", "hazard", "collapse", "deterio", "minimum housing",
)

# Clearly administrative / not a neglect signal — auto-drop, no LLM call needed.
_ADMIN_KEYWORDS = (
    "without permit", "no permit", "permit required", "work without",
    "sign", "banner", "zoning", "land use", "land-use", "setback", "noise", "parking", "license",
    "alarm", "right of way", "right-of-way", "sidewalk", "tree", "temporary",
    "solicit", "vendor", "home occupation", "garage sale", "smoking",
)

_CLASSIFY_SYSTEM = (
    "You classify municipal code-enforcement violation types for a real-estate "
    "investor. You output only valid JSON and never invent types."
)

_CLASSIFY_PROMPT = """\
A real-estate investor wants leads on NEGLECTED / DISTRESSED properties — signs of \
an absentee, overwhelmed, or motivated owner. Classify each code-enforcement \
violation type below.

KEEP (true) when the type indicates physical neglect, deterioration, abandonment, \
accumulation, or a hazardous/unsafe property condition — e.g. tall grass/weeds, \
overgrowth, junk/debris/trash, junked or inoperable vehicles, dilapidated / \
substandard / dangerous / condemned / fire-damaged structures, open / vacant / \
abandoned buildings, property abatement, general nuisance.

DROP (false) when the type is administrative or regulatory and NOT a neglect signal \
— e.g. work or building without a permit, sign/banner violations, \
zoning / land use / setback, noise, parking, business license, alarm permits, \
tree / right-of-way, temporary/solicitation.

Return ONLY this JSON shape:
{{"classifications": [{{"type": "<verbatim type from the list>", "keep": true}}]}}

Violation types:
{types}"""


def is_closed(status: str) -> bool:
    """True if a case status reads as resolved/closed."""
    s = (status or "").lower()
    return any(m in s for m in _CLOSED_STATUS_MARKERS)


# ── Severity ("vexation") tiering ────────────────────────────────────
# Ordered worst → mildest. Rank is used to sort/label; the tier key is stamped
# on NoticeData.severity and surfaced as a `code_severity:<tier>` tag. Grounded
# in the live Austin dataset (6wtj-zbtb), whose OPEN cases carry only ~7 distinct
# `description` values — the two that survive the neglect filter are exactly the
# two worst: failing structures, then city-forced abatement of neglect.
SEVERITY_TIERS = (
    # (tier_key, rank, human_label, description-substring matchers)
    ("structure_condition", 1, "Structure Condition (dangerous/substandard)",
     ("structure condition", "dilapidat", "substandard", "dangerous", "condemn",
      "unsafe", "collapse", "fire damage", "derelict", "minimum housing", "boarded")),
    ("abatement", 2, "Property Abatement (junk/debris/overgrowth)",
     ("abatement", "accumulation", "debris", "rubbish", "junk", "trash",
      "refuse", "dumping", "overgrow", "weed", "grass", "vegetation", "nuisance",
      "unsanitary", "vacant", "abandoned")),
)
_SEVERITY_RANK = {k: r for (k, r, _lbl, _m) in SEVERITY_TIERS}
SEVERITY_LABELS = {k: lbl for (k, _r, lbl, _m) in SEVERITY_TIERS}


def severity_tier(description: str) -> str:
    """Classify a code-enforcement description into a vexation tier key.

    Returns "structure_condition" (worst), "abatement" (high), or "other".
    Structure-condition matchers win over abatement when both hit.
    """
    d = (description or "").lower()
    if not d:
        return "other"
    for tier_key, _rank, _lbl, matchers in SEVERITY_TIERS:
        if any(m in d for m in matchers):
            return tier_key
    return "other"


def severity_rank(tier_key: str) -> int:
    """Numeric rank for sorting (1 = worst); unknown/other sorts last."""
    return _SEVERITY_RANK.get(tier_key, 99)


def stamp_severity(notices: list[NoticeData]) -> None:
    """Set `.severity` on every code_violation notice in place (idempotent)."""
    for n in notices:
        if n.notice_type == "code_violation":
            n.severity = severity_tier(n.violation_description)


def _keyword_verdict(desc: str) -> bool | None:
    """True=keep, False=drop, None=undecided (needs the LLM)."""
    d = (desc or "").lower()
    if not d:
        return None
    # Neglect signals win over admin keywords (e.g. "junk vehicle, no permit").
    if any(k in d for k in _NEGLECT_KEYWORDS):
        return True
    if any(k in d for k in _ADMIN_KEYWORDS):
        return False
    return None


def _llm_classify(types: list[str], api_key: str) -> dict[str, bool]:
    """Classify distinct violation-type strings; returns {type_lower: keep}."""
    verdicts: dict[str, bool] = {}
    for i in range(0, len(types), 100):  # chunk to keep each call bounded
        chunk = types[i:i + 100]
        prompt = _CLASSIFY_PROMPT.format(types="\n".join(f"- {t}" for t in chunk))
        parsed = llm_client.chat_json(
            prompt, system=_CLASSIFY_SYSTEM, max_tokens=2048, api_key=api_key,
        )
        rows = parsed.get("classifications") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            logger.warning("Neglect filter: LLM returned no classifications for a chunk")
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("type"):
                verdicts[str(r["type"]).strip().lower()] = bool(r.get("keep"))
    return verdicts


def filter_code_violations(
    notices: list[NoticeData],
    api_key: str | None = None,
    drop_closed: bool = True,
) -> list[NoticeData]:
    """Keep only OPEN, neglect-signal code_violation records.

    Non-code_violation notices pass through untouched. Controlled by
    config.CODE_VIOLATION_NEGLECT_FILTER (default on).
    """
    if not getattr(config, "CODE_VIOLATION_NEGLECT_FILTER", True):
        return notices

    api_key = api_key or config.ANTHROPIC_API_KEY
    cv = [n for n in notices if n.notice_type == "code_violation"]
    if not cv:
        return notices

    # Gate 1 — drop closed cases.
    dropped_closed = 0
    open_cv: list[NoticeData] = []
    for n in cv:
        if drop_closed and is_closed(n.case_status):
            dropped_closed += 1
        else:
            open_cv.append(n)

    # Gate 2 — resolve a keep/drop verdict per distinct violation type.
    verdicts: dict[str, bool] = {}
    undecided: set[str] = set()
    for n in open_cv:
        desc = (n.violation_description or "").strip()
        kv = _keyword_verdict(desc)
        if kv is None:
            if desc:
                undecided.add(desc)
        else:
            verdicts[desc.lower()] = kv

    use_llm = bool(api_key) and getattr(config, "LLM_BACKEND", "anthropic") == "anthropic"
    if undecided and use_llm:
        verdicts.update(_llm_classify(sorted(undecided), api_key))
    elif undecided:
        logger.warning(
            "Neglect filter: %d violation type(s) unclassified (no LLM) — keeping them",
            len(undecided),
        )

    dropped_type = 0
    kept_cv: list[NoticeData] = []
    for n in open_cv:
        desc = (n.violation_description or "").strip()
        keep = verdicts.get(desc.lower(), True)  # default keep when unresolved
        if keep:
            kept_cv.append(n)
        else:
            dropped_type += 1

    logger.info(
        "Neglect filter: %d/%d code_violation kept (dropped %d closed, %d non-neglect)",
        len(kept_cv), len(cv), dropped_closed, dropped_type,
    )

    # Stamp the vexation tier on every kept case for downstream ranking/tagging.
    stamp_severity(kept_cv)

    # Reassemble preserving the non-code_violation notices.
    kept_ids = {id(n) for n in kept_cv}
    return [n for n in notices if n.notice_type != "code_violation" or id(n) in kept_ids]

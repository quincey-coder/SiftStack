"""DirectSkip batch skip trace — phones + emails for all records.

Runs one synchronous DirectSkip lookup per contact ($0.10/hit, a no-match is
FREE) and populates NoticeData phone/email fields. Runs as a separate pipeline
step before DataSift CSV generation. This replaced the retired Tracerfy batch
tracer (2026-08-20) — same contract, better data (DirectSkip returns the
matched person's relatives too, though this pipeline step only takes the
subject's own phones/emails; the relative graph belongs to the operator-run
skip_orchestrator / deep-prospecting flows).

Signing chain support: traces ALL signing-authority heirs (not just DM #1)
so the user has full contact info for every heir who must sign to close a deal.

Trust boundaries (see src/directskip.py):
  * ResultCode AB1/AB2 = address-only match returning a DIFFERENT person —
    those phones NEVER fill anyone's dial slots.
  * The vendor Deceased flag is an observation, never a verdict.
  * DirectSkip's API is IP-allowlisted — it works from the fixed-IP operator
    box, NOT from Apify (no static egress IP). An auth failure aborts the
    batch loudly (stats["auth_failed"]) instead of burning time per record.
"""

import json
import logging
import re
import time

import config as cfg
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# NoticeData phone/email slots (unchanged from the Tracerfy era — these are the
# NoticeData contract that datasift_formatter reads).
PHONE_FIELDS = [
    "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
    "mobile_5", "landline_1", "landline_2", "landline_3",
]
EMAIL_FIELDS = ["email_1", "email_2", "email_3", "email_4", "email_5"]

_MOBILE_FIELDS = ["mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5"]
_LANDLINE_FIELDS = ["landline_1", "landline_2", "landline_3"]

# Abort the batch after this many consecutive hard failures — an un-allowlisted
# IP or dead key fails every call identically; no point burning the whole loop.
MAX_CONSECUTIVE_FAILURES = 3


# A situs street with no leading house number means vacant land.
_HAS_HOUSE_NUMBER = re.compile(r"^\s*\d")


def _get_contacts_for_trace(
    notice: NoticeData, max_signing_traces: int = 5,
) -> list[tuple[str, str, str, str, str, str]]:
    """Determine who to skip-trace for this notice.

    Returns list of (first_name, last_name, address, city, zip, heir_key).
    heir_key is the full name used to match results back to the right heir.

    For deceased owners: traces DM #1 + all signing-authority heirs with addresses.
    For living owners: traces the property owner only.
    """
    contacts = []

    if (notice.owner_deceased == "yes"
            and notice.decision_maker_name
            and notice.decision_maker_name.strip()):

        # Always include DM #1 (primary contact)
        dm_name = notice.decision_maker_name.strip()
        address = notice.decision_maker_street or notice.address or ""
        city_val = notice.decision_maker_city or notice.city or ""
        zip_code = notice.decision_maker_zip or notice.zip or ""
        first, last, _et = _split_name(dm_name)
        if first and last:
            contacts.append((first, last, address, city_val, zip_code, dm_name))

        # Add other signing-authority heirs from heir_map_json
        if notice.heir_map_json:
            try:
                heirs = json.loads(notice.heir_map_json)
            except (json.JSONDecodeError, TypeError):
                heirs = []

            seen = {dm_name.lower()}
            for heir in heirs:
                if len(contacts) >= max_signing_traces:
                    break
                heir_name = heir.get("name", "").strip()
                if not heir_name or heir_name.lower() in seen:
                    continue
                if not heir.get("signing_authority"):
                    continue
                if heir.get("status") == "deceased":
                    continue
                if not heir.get("street"):
                    continue  # No address = can't trace effectively
                seen.add(heir_name.lower())
                h_first, h_last, _et = _split_name(heir_name)
                if h_first and h_last:
                    contacts.append((
                        h_first, h_last,
                        heir["street"],
                        heir.get("city", ""),
                        heir.get("zip", ""),
                        heir_name,
                    ))
    elif notice.owner_deceased == "yes":
        # Deceased owner with no DM identified → there is no valid LIVING contact
        # to trace. owner_name here is the decedent (the CAD owner-of-record), so
        # skip-tracing it would (a) spend credits on a dead person and (b) produce
        # phones that can't attach to any contact. Leave the record for heir research.
        return []
    else:
        # Living owner — single contact
        name = (notice.owner_name or "").strip()
        if name:
            first, last, _et = _split_name(name)
            if first and last:
                # VACANT LAND: a situs with no house number ("Veldt Dr",
                # "Eggleston St") is almost always raw land — nobody lives
                # there, so anchoring the trace on it finds nothing. The owner
                # is reachable at their MAILING address, which these records do
                # carry (verified on the Travis delinquent roll: all 9
                # house-numberless rows had a full owner mailing address).
                # Chase the mailing address instead; fall back to the property
                # address when there is no mailing address to use.
                street = notice.address or ""
                city_val = notice.city or ""
                zip_code = notice.zip or ""
                mailing = (getattr(notice, "mailing_address", "") or "").strip()
                if mailing and not _HAS_HOUSE_NUMBER.match(street):
                    street = mailing
                    city_val = getattr(notice, "owner_city", "") or city_val
                    zip_code = getattr(notice, "owner_zip", "") or zip_code
                    logger.debug("Anchoring trace on the MAILING address for %s "
                                 "(property %r has no house number)", name, notice.address)
                contacts.append((first, last, street, city_val, zip_code, name))

    return contacts


def _split_name(name: str) -> tuple[str, str, str]:
    """Split a full name into (first, last, entity_type).

    Input is expected in modern `FIRST [MIDDLE] LAST` order (court-source
    names get normalized at the scraper layer). Returns ("", "", entity_type)
    when the name is detected as a government or business entity,
    ("", "", "") when unparseable.
    """
    from notice_parser import _detect_entity_type
    entity_type = _detect_entity_type(name)
    if entity_type:
        return ("", "", entity_type)
    parts = name.strip().split()
    if len(parts) < 2:
        return ("", "", "")
    return (parts[0], parts[-1], "")


# Keep backward-compatible single-contact function for callers that expect it
def _get_contact_for_trace(notice: NoticeData) -> tuple[str, str, str, str, str]:
    """Legacy single-contact wrapper. Returns (first, last, address, city, zip)."""
    contacts = _get_contacts_for_trace(notice, max_signing_traces=1)
    if contacts:
        first, last, addr, city, zip_code, _ = contacts[0]
        return (first, last, addr, city, zip_code)
    return ("", "", "", "", "")


def _lookup_missing_heir_addresses(
    notice: NoticeData, api_key: str | None,
) -> int:
    """Fill in mailing addresses for signing-authority heirs that lack one.

    For each living heir with signing_authority=true but no `street`, runs the
    existing DM address waterfall (TX CAD → Serper/Firecrawl → DDG) and stores
    the result back onto the heir. Mutates notice.heir_map_json in place.

    Returns the number of heirs that gained an address.
    """
    if not notice.heir_map_json:
        return 0
    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(heirs, list):
        return 0

    # Lazy import to avoid a hard dependency cycle on obituary_enricher
    from obituary_enricher import _lookup_dm_address

    city_hint = (notice.city or "").strip()
    filled = 0
    for heir in heirs:
        if not isinstance(heir, dict):
            continue
        if not heir.get("signing_authority"):
            continue
        if heir.get("status") == "deceased":
            continue
        if (heir.get("street") or "").strip():
            continue
        heir_name = (heir.get("name") or "").strip()
        if not heir_name:
            continue

        try:
            addr = _lookup_dm_address(
                heir_name, city_hint, api_key or "", directskip_tier1=False,
            )
        except Exception as e:
            logger.debug("Heir address lookup failed for %s: %s", heir_name, e)
            continue
        if addr and addr.get("street"):
            heir["street"] = addr.get("street", "")
            heir["city"] = addr.get("city", "") or city_hint
            heir["state"] = addr.get("state", "") or "TX"
            heir["zip"] = addr.get("zip", "")
            heir["address_source"] = addr.get("source", "")
            filled += 1
            logger.info(
                "  Heir address filled: %s → %s, %s",
                heir_name, heir["street"], heir.get("city", ""),
            )

    if filled:
        notice.heir_map_json = json.dumps(heirs, ensure_ascii=False)
    return filled


def batch_skip_trace(
    notices: list[NoticeData],
    max_signing_traces: int = 5,
    lookup_heir_addresses: bool = True,
    address_lookup_api_key: str | None = None,
    max_cost: float | None = None,
) -> dict:
    """Run DirectSkip skip trace on all records (one API call per contact).

    For deceased owners, traces ALL signing-authority heirs (up to
    max_signing_traces per property). DM #1's phones go to flat NoticeData
    fields; other heirs' phones/emails are stored in their heir_map_json entry.

    When lookup_heir_addresses is True, signing-authority heirs without a known
    mailing address get one looked up (TX CAD → people search) before the trace
    so DirectSkip has enough info to return a confident match. Uses
    ANTHROPIC_API_KEY (or the explicit override) for LLM-based extraction from
    people-search pages.

    MONEY: DirectSkip bills $DIRECTSKIP_COST_PER_HIT per MATCH; a no-match is
    free. The loop stops before any call that could push spend past the cap
    (max_cost, default cfg.MAX_DIRECTSKIP_COST_USD).

    Returns stats dict: {total, submitted, matched, phones_found, emails_found,
                         cost, signing_heirs_traced, heir_addresses_filled,
                         cost_capped, auth_failed}.
    """
    stats = {
        "total": len(notices),
        "submitted": 0,
        "matched": 0,
        "phones_found": 0,
        "emails_found": 0,
        "cost": 0.0,
        "signing_heirs_traced": 0,
        "heir_addresses_filled": 0,
        "cost_capped": False,
        "auth_failed": False,
    }

    if not cfg.DIRECTSKIP_API_KEY:
        logger.warning("DirectSkip API key not set — skipping batch skip trace")
        return stats

    from directskip import (
        BATCH_CALL_DELAY, COST_PER_HIT, DirectSkipClient, DirectSkipError,
        parse_api_response,
    )

    cap = float(cfg.MAX_DIRECTSKIP_COST_USD if max_cost is None else max_cost)

    # Fill missing heir addresses BEFORE building the trace batch — otherwise
    # those heirs get silently dropped at the `if not heir.get("street")` check
    # in _get_contacts_for_trace and never get DirectSkip phones.
    if lookup_heir_addresses:
        llm_key = address_lookup_api_key or getattr(cfg, "ANTHROPIC_API_KEY", "") or None
        for notice in notices:
            if notice.owner_deceased != "yes":
                continue
            try:
                stats["heir_addresses_filled"] += _lookup_missing_heir_addresses(notice, llm_key)
            except Exception:
                logger.exception("Heir address lookup pass failed for notice")
        if stats["heir_addresses_filled"]:
            logger.info("Heir address backfill: %d heir(s) gained an address",
                        stats["heir_addresses_filled"])

    # Build lookup map: list of (notice, first, last, address, city, zip, heir_key)
    # Multiple entries per notice for signing-authority heirs
    lookup_map: list[tuple[NoticeData, str, str, str, str, str, str]] = []
    for notice in notices:
        contacts = _get_contacts_for_trace(notice, max_signing_traces)
        for i, (first, last, address, city, zip_code, heir_key) in enumerate(contacts):
            # Skip DM #1 if already has phones
            if i == 0 and notice.primary_phone:
                continue
            # Skip heirs already traced (have phones in heir_map_json)
            if i > 0 and _heir_has_phones(notice, heir_key):
                continue
            lookup_map.append((notice, first, last, address, city, zip_code, heir_key))

    if not lookup_map:
        logger.info("DirectSkip: no records to skip-trace (all have phones or no valid names)")
        return stats

    stats["signing_heirs_traced"] = sum(
        1 for n, _, _, _, _, _, hk in lookup_map
        if n.decision_maker_name and hk != n.decision_maker_name
    )
    logger.info("DirectSkip batch: %d contacts (%d notices, %d signing heirs) — "
                "worst case $%.2f at $%.2f/hit, cap $%.2f",
                len(lookup_map),
                len(set(id(n) for n, *_ in lookup_map)),
                stats["signing_heirs_traced"],
                len(lookup_map) * COST_PER_HIT, COST_PER_HIT, cap)

    client = DirectSkipClient()
    consecutive_failures = 0

    for notice, first, last, address, city, zip_code, heir_key in lookup_map:
        if round(stats["cost"] + COST_PER_HIT, 2) > cap:
            stats["cost_capped"] = True
            logger.warning(
                "DirectSkip: cost cap $%.2f reached after %d hit(s) — stopping. "
                "Raise MAX_DIRECTSKIP_COST_USD (or max_cost) to trace more.",
                cap, stats["matched"],
            )
            break

        stats["submitted"] += 1
        try:
            data = client.search_contact(
                first=first, last=last,
                mailing_address=address, mailing_city=city,
                mailing_state=notice.state or "TX", mailing_zip=zip_code,
                property_address=notice.address or "",
                property_city=notice.city or "",
                property_state=notice.state or "TX",
                property_zip=notice.zip or "",
            )
        except DirectSkipError as e:
            consecutive_failures += 1
            msg = str(e)
            if ("No DirectSkip API key" in msg or "status.error" in msg
                    or consecutive_failures >= MAX_CONSECUTIVE_FAILURES):
                stats["auth_failed"] = True
                logger.error(
                    "DirectSkip batch ABORTED after %d failure(s): %s "
                    "(is this machine's IP allowlisted with support@directskip.com? "
                    "The API does not work from Apify — no static egress IP.)",
                    consecutive_failures, msg,
                )
                break
            logger.warning("DirectSkip lookup failed for %s %s: %s — continuing",
                           first, last, msg)
            continue
        consecutive_failures = 0

        record = parse_api_response(data)
        if record.no_match:
            continue  # free — no charge on a miss
        stats["cost"] = round(stats["cost"] + COST_PER_HIT, 2)

        if record.address_only_match:
            # AB1/AB2 — the matched person is NOT who we asked for. Their
            # phones must never land on this contact. Billed, but unusable.
            logger.info("    %s %s: address-only match (%s) — discarded",
                        first, last, record.result_code)
            continue

        phones = [p["number"] for p in record.subject_phones if p.get("type") == "mobile"]
        phones += [p["number"] for p in record.subject_phones if p.get("type") != "mobile"]
        emails = list(record.subject_emails)
        if not phones and not emails:
            continue

        # Is this the primary DM (#1) / living owner?
        is_primary = (
            notice.decision_maker_name
            and heir_key.lower() == notice.decision_maker_name.strip().lower()
        ) or notice.owner_deceased != "yes"

        if is_primary and not notice.primary_phone:
            _fill_flat_slots(notice, record.subject_phones, emails)
            if record.deceased_flag and hasattr(notice, "smartskip_deceased_flag"):
                # Observation only — never sets owner_deceased (research decides).
                notice.smartskip_deceased_flag = "yes"
        elif not is_primary:
            _store_heir_phones(notice, heir_key, phones, emails)

        stats["matched"] += 1
        stats["phones_found"] += len(phones)
        stats["emails_found"] += len(emails)
        logger.info("    %s %s: %d phones, %d emails%s",
                    first, last, len(phones), len(emails),
                    " (signing heir)" if not is_primary else "")
        if BATCH_CALL_DELAY:
            time.sleep(BATCH_CALL_DELAY)

    logger.info("  DirectSkip batch complete: %d/%d matched, %d phones, %d emails, $%.2f",
                stats["matched"], stats["submitted"],
                stats["phones_found"], stats["emails_found"], stats["cost"])
    return stats


def _fill_flat_slots(notice: NoticeData, typed_phones: list[dict],
                     emails: list[str]) -> None:
    """Write the subject's phones/emails onto the flat NoticeData slots.

    Mobiles land in mobile_1..5, landlines/other in landline_1..3, and the
    best number (mobile-first) becomes primary_phone.
    """
    mobiles = [p["number"] for p in typed_phones if p.get("type") == "mobile"]
    others = [p["number"] for p in typed_phones if p.get("type") != "mobile"]
    ordered = mobiles or others
    if ordered and not notice.primary_phone:
        notice.primary_phone = ordered[0]
    for field, number in zip(_MOBILE_FIELDS, mobiles):
        if not getattr(notice, field, ""):
            setattr(notice, field, number)
    for field, number in zip(_LANDLINE_FIELDS, others):
        if not getattr(notice, field, ""):
            setattr(notice, field, number)
    for field, email in zip(EMAIL_FIELDS, emails):
        if not getattr(notice, field, ""):
            setattr(notice, field, email)


def _heir_has_phones(notice: NoticeData, heir_key: str) -> bool:
    """Check if a specific heir already has phone data in heir_map_json."""
    if not notice.heir_map_json:
        return False
    try:
        heirs = json.loads(notice.heir_map_json)
        for h in heirs:
            if h.get("name", "").lower() == heir_key.lower():
                return bool(h.get("phones"))
    except (json.JSONDecodeError, TypeError):
        pass
    return False


def _store_heir_phones(
    notice: NoticeData, heir_key: str,
    phones: list[str], emails: list[str],
) -> None:
    """Store phones/emails on a specific heir's entry in heir_map_json."""
    if not notice.heir_map_json:
        return
    try:
        heirs = json.loads(notice.heir_map_json)
        for h in heirs:
            if h.get("name", "").lower() == heir_key.lower():
                h["phones"] = phones
                h["emails"] = emails
                break
        notice.heir_map_json = json.dumps(heirs, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass

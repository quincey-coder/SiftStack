"""Enformion Business Search V2 client — principals behind entity-owned property.

This is the ONE piece of Enformion that Deep Prospecting v5 retains. The
Enformion *Person* search is retired (see ``smartskip``), but entity owners have
no other path: SmartSkip requires a first and last name, and Tracerfy is
consumer-only, so every LLC / trust / estate is invisible to the heir engine.
Entities are a real share of a distressed pull, not an edge case.

Why this matters next to ``entity_researcher``: that module infers the person
behind an entity from DuckDuckGo snippets with an LLM, which is the same
"model names a real person from prose" shape that produced the obituary heir
hallucination. BusinessV2 returns the actual Secretary-of-State corp filing, so
an officer it names is GROUNDED in a public registry record. Use this first and
keep the web/LLM path as the fallback when the registry has nothing.

API contract (verified upstream live 2026-07-22):
- POST https://devapi.enformion.com/BusinessV2Search with header
  ``galaxy-search-type: BusinessV2``. The v1 ``BusinessSearch`` type returns
  "Access denied" on this account, and ``AddressSearch`` is not licensed at all.
- Response: ``businessV2Records[]``, each carrying ``usCorpFilings[]`` (SOS corp
  records: officers, with titles like REGISTERED AGENT) and/or
  ``newBusinessFilings[]`` (contacts + mail/business addresses).
- Registered agents are usually commercial fronts, not principals. When no human
  surfaces, the fallback is a reverse-address unmask: if the entity's mailing
  address is a RESIDENCE, whoever owns that residence is very likely the
  principal.
"""

from __future__ import annotations

import logging

import requests

import config as cfg

logger = logging.getLogger(__name__)

BUSINESS_V2_URL = "https://devapi.enformion.com/BusinessV2Search"
TIMEOUT = 60

# Commercial registered-agent fronts that are NOT principals. ZenBusiness is
# Austin-based and extremely common on Texas LLCs; CSC and CT Corporation are the
# usual institutional filers.
AGENT_FRONTS = (
    "REGISTERED AGENT", "NORTHWEST", "CORPORATION AGENTS", "CORPORATE DIRECT",
    "REGISTERED AGENTS INC", "CT CORPORATION", "COGENCY", "INCORP",
    "CORPORATION SERVICE COMPANY", "NATIONAL REGISTERED AGENTS", "VCORP",
    "LEGALZOOM", "HARBOR COMPLIANCE", "ZENBUSINESS", "TEXAS REGISTERED AGENT",
    "CAPITOL CORPORATE SERVICES", "CSC ",
)

# Tokens that mean the "officer" is another company, not a human.
_ENTITY_TOKENS = ("LLC", "INC", "L.L.C", "CORP", "COMPANY", "TRUST", "LP", "L.P",
                  "PLLC", "LTD", "HOLDINGS", "PARTNERS")


def is_configured() -> bool:
    return bool(getattr(cfg, "ENFORMION_AP_NAME", "") and
                getattr(cfg, "ENFORMION_AP_PASSWORD", ""))


def business_search(name: str, city_state: str = "", *, results: int = 3) -> list[dict]:
    """One BusinessV2 search. Returns businessV2Records (possibly empty)."""
    if not is_configured():
        logger.debug("Enformion not configured — skipping BusinessV2 search for %s", name)
        return []
    body: dict = {"BusinessName": name, "Page": 1, "ResultsPerPage": results}
    if city_state:
        body["Addresses"] = [{"AddressLine2": city_state}]
    try:
        resp = requests.post(BUSINESS_V2_URL, json=body, timeout=TIMEOUT, headers={
            "galaxy-ap-name": cfg.ENFORMION_AP_NAME,
            "galaxy-ap-password": cfg.ENFORMION_AP_PASSWORD,
            "galaxy-search-type": "BusinessV2",
            "Content-Type": "application/json",
        })
    except requests.RequestException as exc:
        logger.warning("BusinessV2 request failed for %s: %s", name, exc)
        return []
    if resp.status_code != 200:
        logger.warning("BusinessV2 HTTP %s for %s: %s", resp.status_code, name, resp.text[:200])
        return []
    try:
        return resp.json().get("businessV2Records") or []
    except ValueError:
        logger.warning("BusinessV2 returned non-JSON for %s", name)
        return []


def extract_officers(records: list[dict], entity_name: str) -> list[dict]:
    """Pull human officers/contacts from corp + new-business filings.

    Filters out entity self-references and commercial registered-agent fronts.
    Returns [{name, title, address, source}] deduped by name.
    """
    tokens = entity_name.split()
    token = tokens[0].upper() if tokens else ""
    seen: set[str] = set()
    officers: list[dict] = []

    for rec in records:
        filings = (rec.get("usCorpFilings") or []) + (rec.get("newBusinessFilings") or [])
        for filing in filings:
            fname = (filing.get("name") or filing.get("company") or "").upper()
            # Guard against a fuzzy match pulling in an unrelated company.
            if token and token not in fname:
                continue
            for off in (filing.get("officers") or []) + (filing.get("contacts") or []):
                n = off.get("name") or {}
                full = (n.get("nameRaw") or n.get("fullName") or "").strip()
                upper = full.upper()
                if not full or upper in seen:
                    continue
                if any(t in upper for t in _ENTITY_TOKENS):
                    continue
                if any(f in upper for f in AGENT_FRONTS):
                    continue
                seen.add(upper)
                title = off.get("title") or off.get("officerTitleDesc") or ""
                officers.append({
                    "name": full,
                    "title": title,
                    "address": (off.get("address") or {}).get("fullAddress") or "",
                    "source": "enformion_businessv2",
                    # Flagged, NOT dropped: on a small family LLC the registered
                    # agent is usually the owner, while on a large one it is a
                    # paid front. The caller decides; we only mark it. (Match is
                    # loose because the feed contains typos like "REGISTRED AGENT".)
                    "is_registered_agent": _is_agent_title(title),
                })

    # Real principals first, registered agents last.
    officers.sort(key=lambda o: o["is_registered_agent"])
    return officers


def _is_agent_title(title: str) -> bool:
    t = (title or "").upper()
    return "AGENT" in t and ("REGIST" in t or "RESIDENT" in t or "STATUTORY" in t)


def find_principals(entity_name: str, city_state: str = "TX") -> list[dict]:
    """Search + extract in one call. Empty list means 'registry had no human'."""
    officers = extract_officers(business_search(entity_name, city_state), entity_name)
    if officers:
        logger.info("BusinessV2: %d principal(s) for %s", len(officers), entity_name)
    else:
        logger.info("BusinessV2: no human principal for %s — fall back to a "
                    "reverse-address unmask of the entity's mailing address", entity_name)
    return officers


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Look up the principals behind an entity")
    parser.add_argument("entity", help='entity name, e.g. "S & B UNLIMITED LLC"')
    parser.add_argument("--city-state", default="TX")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if not is_configured():
        raise SystemExit("ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD not set in .env")
    print(json.dumps(find_principals(args.entity, args.city_state), indent=2))

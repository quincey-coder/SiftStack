"""DirectSkip skip-trace client — single-record API (primary) + portal fallback.

DirectSkip returns a traced owner PLUS their relatives (each with phone numbers)
for one property in a single synchronous lookup. Unlike SmartSkip's batch
submit/pay/download lifecycle, the DirectSkip API is one HTTP POST per person,
so this client follows the stateless per-record shape of ``tracerfy_skip_tracer``
/ ``phone_validator`` (static key, per-call, hard cost cap) rather than the
stateful ``SmartSkipClient``.

Two transports, one normalized output (``DirectSkipRecord``):
  * **API** (default) — ``POST https://api0.directskip.com/v2/search_contact.php``.
    Auth = ``api_key`` in the JSON body; the caller's PUBLIC IP must be
    allowlisted with support@directskip.com. Verified live 2026-08-17.
  * **Portal** (fallback) — the ``app.directskip.com`` upload wizard (batch,
    cookie auth, not IP-bound). Use for large batches or where the API IP
    allowlist can't be satisfied (e.g. Apify, which has no static egress IP).

MONEY SAFETY — read before using:
  * Billing is **pay-per-hit**: a no-match (empty ``contacts``) bills NOTHING
    (verified live). Only a matched record costs ``COST_PER_HIT``.
  * ``search`` is a single lookup (one hit max). ``batch_search`` enforces a hard
    ``MAX_DIRECTSKIP_COST_USD`` cap and never issues a call that could push spend
    past it, plus a per-call delay to stay under rate limits.
  * The portal submit wizard's step-4 confirm is the only token-spending portal
    call; it is gated on an ``orders.json`` ledger entry + a matching
    ``--confirm-rows`` (mirrors ``smartskip.pay``) and is never called implicitly.

Trust boundaries (shared with SmartSkip — see that module's docstring):
  * ``Deceased`` is an OBSERVATION, never a verdict: this never sets
    ``owner_deceased`` / ``date_of_death``. Death stays the research layer's call.
  * ``ResultCode`` ``AB1``/``AB2`` = an ADDRESS-ONLY match that returns a
    DIFFERENT person than the input (e.g. input Barbara Smith -> returned Donald
    Smith). That person is never treated as the owner and never fills a dial slot.
  * Relationship labels are coarse; everyone maps to ``relative`` (no signing
    authority) until the research layer confirms it.

CLI:
    python src/directskip.py search  --owner "Jane Doe" --property-address "123 Main St" \
                                     --property-city Austin --property-state TX --property-zip 78701
    python src/directskip.py batch   --input owners.csv --max-cost 5.00 --apply-export out.jsonl
    python src/directskip.py parse   --export result.json        # or a portal contactinfo CSV
    python src/directskip.py balance                             # portal credit balance
    python src/directskip.py list                                # portal orders (fallback)
    python src/directskip.py download --id <code> --out file.csv # portal result (fallback)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

import config

# Reuse SmartSkip's hygiene + bridge helpers verbatim — no divergent copies.
from smartskip import (
    _MOBILE_SLOTS,
    _LANDLINE_SLOTS,
    _absurd_age,
    _addr_key,
    _dedupe_phones,
    _digits,
    _split_owner_name,
    canon_relationship,
)

logger = logging.getLogger(__name__)

# ── API transport ─────────────────────────────────────────────────────
API_URL = "https://api0.directskip.com/v2/search_contact.php"
API_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
# Serialize API calls with at least this gap — the portal rate-limited a rapid
# burst during discovery, so stay conservative.
BATCH_CALL_DELAY = 1.0

COST_PER_HIT = float(getattr(config, "DIRECTSKIP_COST_PER_HIT", 0.10))

# ── Portal transport ──────────────────────────────────────────────────
PORTAL_BASE = "https://app.directskip.com/"
PORTAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MAX_RELATIVES = 5   # DirectSkip returns up to 5 relatives per person
MAX_PHONES = 7      # up to 7 phones on the primary, 5 per relative

_RUN_DIR = Path(getattr(config, "PROJECT_ROOT", Path.cwd())) / "data" / "directskip"
SESSION_FILE = _RUN_DIR / "session.json"
ORDERS_FILE = _RUN_DIR / "orders.json"

# Portal contactinfo CSV columns that MUST be present, else the layout drifted
# and every column would misalign. Same loud gate as clean_directskip.py.
_CSV_REQUIRED = ["Input Property Address", "ResultCode", "Phone1", "Relative1 Name"]


class DirectSkipError(RuntimeError):
    """Any DirectSkip failure. Raised loudly — never swallowed into a zero result."""


# ── Normalized records ────────────────────────────────────────────────

@dataclass
class DirectSkipPerson:
    """One person (owner, additional resident, or relative) from a result."""
    first: str = ""
    last: str = ""
    name: str = ""
    relationship_raw: str = ""      # coarse vendor label, if any
    relationship: str = "relative"  # canonical; feeds rank_decision_makers
    age: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phones: list[dict] = field(default_factory=list)   # [{number, type}]
    deceased_flag: bool = False     # vendor flag — OBSERVATION only


@dataclass
class DirectSkipRecord:
    """One traced owner plus their relative graph, from either transport."""
    input_name: str = ""
    first: str = ""
    last: str = ""
    property_address: str = ""
    property_city: str = ""
    property_state: str = ""
    property_zip: str = ""
    mailing_address: str = ""
    mailing_city: str = ""
    mailing_state: str = ""
    mailing_zip: str = ""
    result_code: str = ""
    deceased_flag: bool = False
    address_only_match: bool = False   # AB1/AB2 — matched person is NOT the owner
    subject_phones: list[dict] = field(default_factory=list)
    subject_emails: list[str] = field(default_factory=list)
    confirmed_address: str = ""
    relatives: list[DirectSkipPerson] = field(default_factory=list)
    source: str = "directskip"         # transport tag: "api" or "csv" set on parse

    @property
    def has_results(self) -> bool:
        return bool(self.subject_phones) or any(r.phones for r in self.relatives)

    @property
    def no_match(self) -> bool:
        return not self.subject_phones and not self.relatives and not self.first and not self.last


# ── shared normalizers ────────────────────────────────────────────────

def _phone_type(raw: str) -> str:
    """Normalize a vendor phone type to 'mobile' / 'landline' / 'other'."""
    t = (raw or "").strip().lower()
    if t.startswith("mobile"):
        return "mobile"
    if t.startswith("residential") or t.startswith("landline"):
        return "landline"
    return "other"


def _truthy_deceased(raw) -> bool:
    return str(raw or "").strip().upper() in ("Y", "YES", "TRUE", "1")


def _clean(raw) -> str:
    return str(raw or "").strip()


def _result_code_str(raw) -> str:
    """Extract the result code from the API's ``result_code`` (str | list | dict)."""
    if isinstance(raw, str):
        return raw.strip().upper()
    if isinstance(raw, dict):
        return _clean(raw.get("result_code")).upper()
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return _clean(first.get("result_code")).upper()
        return _clean(first).upper()
    return ""


# ── Client ────────────────────────────────────────────────────────────

class DirectSkipClient:
    """DirectSkip access over the API (default) or the portal (fallback)."""

    def __init__(self, api_key: str = "", email: str = "", password: str = "",
                 transport: str = "api", run_dir: Path | None = None):
        self.api_key = api_key or getattr(config, "DIRECTSKIP_API_KEY", "")
        self.email = email or getattr(config, "DIRECTSKIP_EMAIL", "")
        self.password = password or getattr(config, "DIRECTSKIP_PASSWORD", "")
        self.transport = transport
        self.run_dir = Path(run_dir) if run_dir else _RUN_DIR
        self.orders_file = self.run_dir / "orders.json"
        self._session: requests.Session | None = None   # lazy portal session

    # ── API transport (primary) ──
    def search_contact(self, *, first: str, last: str,
                       mailing_address: str, mailing_city: str, mailing_state: str, mailing_zip: str,
                       property_address: str, property_city: str, property_state: str, property_zip: str,
                       custom_field_1: str = "", custom_field_2: str = "", custom_field_3: str = "",
                       dnc_scrub: bool = False, owner_fix: bool = False,
                       auto_match_boost: bool = True) -> dict:
        """Single synchronous lookup. Returns the raw JSON dict.

        A no-match returns ``{status:{error:""}, ..., contacts:[]}`` and bills
        nothing. Any non-empty ``status.error`` raises ``DirectSkipError``.
        """
        if not self.api_key:
            raise DirectSkipError(
                "No DirectSkip API key. Set DIRECTSKIP_API_KEY in .env "
                "(and register this machine's public IP with support@directskip.com)."
            )
        body = {
            "api_key": self.api_key,
            "first_name": first, "last_name": last,
            "mailing_address": mailing_address, "mailing_city": mailing_city,
            "mailing_state": mailing_state, "mailing_zip": mailing_zip,
            "property_address": property_address, "property_city": property_city,
            "property_state": property_state, "property_zip": property_zip,
            "custom_field_1": custom_field_1, "custom_field_2": custom_field_2,
            "custom_field_3": custom_field_3,
            "auto_match_boost": 1 if auto_match_boost else 0,
            "dnc_scrub": 1 if dnc_scrub else 0,
            "owner_fix": 1 if owner_fix else 0,
        }
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(API_URL, json=body, headers=API_HEADERS,
                                     timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_err = exc
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF ** attempt)
                    continue
                raise DirectSkipError(f"request failed after {MAX_RETRIES} tries: {exc}")
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF ** attempt)
                    continue
                raise DirectSkipError(f"search_contact failed ({last_err})")
            try:
                data = resp.json()
            except ValueError:
                raise DirectSkipError(f"non-JSON response: {resp.text[:200]}")
            err = _clean((data.get("status") or {}).get("error"))
            if err:
                raise DirectSkipError(f"API returned status.error={err!r} "
                                      "(often an un-allowlisted IP or a bad key)")
            return data
        raise DirectSkipError(f"search_contact failed: {last_err}")

    def search_record(self, notice, **flags) -> DirectSkipRecord | None:
        """Look up one NoticeData record. Returns None for entities (not traceable)."""
        if getattr(notice, "business_name", "") or getattr(notice, "entity_type", ""):
            return None
        first, last = _split_owner_name(getattr(notice, "owner_name", ""))
        if not (first and last):
            return None
        data = self.search_contact(
            first=first, last=last,
            mailing_address=getattr(notice, "mailing_address", "") or getattr(notice, "address", ""),
            mailing_city=getattr(notice, "owner_city", "") or getattr(notice, "city", ""),
            mailing_state=getattr(notice, "owner_state", "") or getattr(notice, "state", "TX"),
            mailing_zip=getattr(notice, "owner_zip", "") or getattr(notice, "zip", ""),
            property_address=getattr(notice, "address", ""),
            property_city=getattr(notice, "city", ""),
            property_state=getattr(notice, "state", "TX"),
            property_zip=getattr(notice, "zip", ""),
            custom_field_1=getattr(notice, "parcel_id", "") or "",
            **flags,
        )
        return parse_api_response(data)

    def batch_search(self, notices, max_cost: float | None = None,
                     delay: float = BATCH_CALL_DELAY, **flags) -> dict:
        """Trace many NoticeData records with a hard cost cap. Applies results.

        Only a HIT (non-empty contacts) costs money, so the loop checks
        ``spent + COST_PER_HIT <= cap`` before each call and stops cleanly when
        the next possible hit would exceed the cap. Returns aggregate stats.
        """
        cap = config.MAX_DIRECTSKIP_COST_USD if max_cost is None else float(max_cost)
        stats = {"total": len(notices), "traced": 0, "matched": 0, "no_match": 0,
                 "entities_skipped": 0, "unusable_name": 0, "heirs": 0,
                 "relative_phones": 0, "cost": 0.0, "cap": cap, "cap_reached": False}

        for notice in notices:
            if getattr(notice, "business_name", "") or getattr(notice, "entity_type", ""):
                stats["entities_skipped"] += 1
                continue
            first, last = _split_owner_name(getattr(notice, "owner_name", ""))
            if not (first and last):
                stats["unusable_name"] += 1
                continue
            if round(stats["cost"] + COST_PER_HIT, 2) > cap:
                stats["cap_reached"] = True
                logger.warning("DirectSkip: cost cap $%.2f reached after %d hit(s); "
                               "stopping before the next lookup.", cap, stats["matched"])
                break

            record = self.search_record(notice, **flags)
            stats["traced"] += 1
            if record is None or record.no_match:
                stats["no_match"] += 1
                continue
            stats["matched"] += 1
            stats["cost"] = round(stats["cost"] + COST_PER_HIT, 2)
            applied = apply_to_notice(notice, record)
            stats["heirs"] += applied["heirs"]
            stats["relative_phones"] += applied["relative_phones"]
            if delay:
                time.sleep(delay)

        logger.info("DirectSkip: traced %d/%d — %d matched (%d no-match), "
                    "%d heirs, $%.2f spent (cap $%.2f%s)",
                    stats["traced"], stats["total"], stats["matched"], stats["no_match"],
                    stats["heirs"], stats["cost"], cap,
                    ", CAP REACHED" if stats["cap_reached"] else "")
        return stats

    # ── Portal transport (fallback) ──
    def _portal(self) -> requests.Session:
        if self._session is not None:
            return self._session
        s = requests.Session()
        s.headers["User-Agent"] = PORTAL_UA
        self._session = s
        self._portal_login(s)
        return s

    def _portal_login(self, s: requests.Session) -> None:
        if not (self.email and self.password):
            raise DirectSkipError("No portal credentials. Set DIRECTSKIP_EMAIL / "
                                  "DIRECTSKIP_PASSWORD in .env for the portal fallback.")
        s.get(PORTAL_BASE + "login.php", timeout=REQUEST_TIMEOUT)
        s.post(PORTAL_BASE + "login.php",
               data={"email": self.email, "password": self.password, "submit": "Login"},
               timeout=REQUEST_TIMEOUT)
        check = s.get(PORTAL_BASE + "files.php", timeout=REQUEST_TIMEOUT)
        if "login.php" in check.url:
            raise DirectSkipError("Portal login failed — check DIRECTSKIP_EMAIL / DIRECTSKIP_PASSWORD.")

    def _portal_get(self, path: str) -> requests.Response:
        """GET a portal page, re-logging in once if the session expired."""
        s = self._portal()
        resp = s.get(PORTAL_BASE + path, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if "login.php" in resp.url and not path.startswith("login"):
            self._portal_login(s)
            resp = s.get(PORTAL_BASE + path, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return resp

    def list_orders(self) -> list[dict]:
        """Parse the portal's files.php order table. Fails loudly if it drifts."""
        html = self._portal_get("files.php").text
        heads = re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)
        if not any("Skip Hits" in re.sub(r"<[^>]+>", "", h) for h in heads):
            raise DirectSkipError("files.php layout changed — 'Skip Hits' column not found. "
                                  "Refusing to guess columns.")
        orders = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) < 8 or not cells[0].isdigit():
                continue
            dl = re.search(r'href="(download\.php\?code=[^"]+)"', row)
            orders.append({
                "id": cells[0], "date": cells[1], "name": cells[2],
                "skip_searches": cells[4], "skip_hits": cells[5],
                "hit_rate": cells[6], "status": cells[7],
                "download": dl.group(1) if dl else "",
            })
        logger.info("DirectSkip portal: %d order(s) listed", len(orders))
        return orders

    def download(self, code: str, out_path: str | Path) -> Path:
        """Download a portal result CSV by its download.php code (or an order id)."""
        if code.isdigit():
            match = next((o for o in self.list_orders() if o["id"] == code), None)
            if not match or not match["download"]:
                raise DirectSkipError(f"No downloadable result for order id {code}.")
            code = match["download"].split("code=", 1)[1]
        resp = self._portal_get(f"download.php?code={code}")
        if "login.php" in resp.url or resp.status_code != 200:
            raise DirectSkipError(f"download failed ({resp.status_code}) for code {code}.")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        logger.info("DirectSkip portal: saved %s (%d bytes)", out_path, len(resp.content))
        return out_path

    def balance(self) -> dict:
        """Read the portal credit balances (index.php). No API balance endpoint exists."""
        text = re.sub(r"<[^>]+>", " ", self._portal_get("index.php").text)

        def grab(label):
            m = re.search(re.escape(label) + r"[^0-9$]*([\d,.]+)", text)
            return m.group(1) if m else None

        return {
            "per_hit_rate": grab("Per Hit Rate"),
            "prepaid_hit_credits": grab("PrePaid Hit Credits"),
            "download_credits": grab("Download Credits"),
            "searches_last_30": grab("Searches Last 30"),
        }


# ── Response / export parsing ─────────────────────────────────────────

def _person_from(first, last, ptype, age, phones, *, street="", city="", state="",
                 zip_code="", deceased=False) -> DirectSkipPerson:
    first, last = _clean(first), _clean(last)
    return DirectSkipPerson(
        first=first, last=last, name=f"{first} {last}".strip(),
        relationship_raw=_clean(ptype), relationship=canon_relationship(ptype),
        age=_clean(age), street=_clean(street), city=_clean(city),
        state=_clean(state), zip=_clean(zip_code), phones=phones,
        deceased_flag=bool(deceased),
    )


def _split_relative_name(name: str) -> tuple[str, str]:
    """Split a single relative 'Name' into (first, last), conservatively.

    Relatives arrive as one string, sometimes 'LAST, FIRST' and sometimes
    'FIRST LAST'. Comma form is unambiguous; otherwise first token = first name.
    """
    n = _clean(name)
    if not n:
        return ("", "")
    if "," in n:
        last, _, first = n.partition(",")
        first = first.strip().split(" ")[0]
        return (first, last.strip())
    parts = [p for p in re.split(r"\s+", n) if p]
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], parts[-1])


def _api_phones(items) -> list[dict]:
    out = []
    for p in items or []:
        num = _digits(p.get("phonenumber") if isinstance(p, dict) else p)
        if num:
            ptype = p.get("phonetype") if isinstance(p, dict) else ""
            out.append({"number": num, "type": _phone_type(ptype)})
    return _dedupe_phones(out)


def parse_api_response(data: dict) -> DirectSkipRecord:
    """Normalize one ``search_contact`` JSON response into a DirectSkipRecord."""
    inp = data.get("input") or {}
    code = _result_code_str(data.get("result_code"))
    rec = DirectSkipRecord(
        input_name=f"{_clean(inp.get('firstname'))} {_clean(inp.get('lastname'))}".strip(),
        property_address=_clean(inp.get("property_address")),
        property_city=_clean(inp.get("property_city")),
        property_state=_clean(inp.get("property_state")),
        property_zip=_clean(inp.get("property_zip")),
        mailing_address=_clean(inp.get("address")),
        mailing_city=_clean(inp.get("city")),
        mailing_state=_clean(inp.get("state")),
        mailing_zip=_clean(inp.get("zip")),
        result_code=code,
        address_only_match=code in ("AB1", "AB2"),
        source="api",
    )

    contacts = data.get("contacts") or []
    if not contacts:
        return rec

    primary = contacts[0] or {}
    names = primary.get("names") or []
    if names:
        rec.first = _clean(names[0].get("firstname"))
        rec.last = _clean(names[0].get("lastname"))
        rec.deceased_flag = _truthy_deceased(names[0].get("deceased"))
    rec.subject_phones = _api_phones(primary.get("phones"))
    rec.subject_emails = [e for e in (
        _clean(x.get("email") if isinstance(x, dict) else x).lower()
        for x in (primary.get("emails") or [])) if e]
    conf = primary.get("confirmed_address") or []
    if conf:
        c = conf[0] if isinstance(conf, list) else conf
        if isinstance(c, dict):
            rec.confirmed_address = ", ".join(
                v for v in [_clean(c.get("address")), _clean(c.get("city")),
                            _clean(c.get("state")), _clean(c.get("zip"))] if v)

    # Owner's relatives.
    for rel in primary.get("relatives") or []:
        first, last = _split_relative_name(rel.get("name"))
        if not (first or last):
            continue
        rec.relatives.append(_person_from(
            first, last, "", rel.get("age"), _api_phones(rel.get("phones"))))

    # Additional matched people (co-owners / residents) + their relatives — all
    # go into the relative graph so their phones never reach the owner dial slots.
    for extra in contacts[1:]:
        enames = extra.get("names") or []
        if enames:
            rec.relatives.append(_person_from(
                enames[0].get("firstname"), enames[0].get("lastname"), "",
                enames[0].get("age"), _api_phones(extra.get("phones")),
                deceased=_truthy_deceased(enames[0].get("deceased"))))
        for rel in extra.get("relatives") or []:
            first, last = _split_relative_name(rel.get("name"))
            if first or last:
                rec.relatives.append(_person_from(
                    first, last, "", rel.get("age"), _api_phones(rel.get("phones"))))
    return rec


def _csv_phones(row: dict, prefix: str, count: int) -> list[dict]:
    out = []
    for i in range(1, count + 1):
        num = _digits(row.get(f"{prefix}Phone{i}", ""))
        if num:
            out.append({"number": num, "type": _phone_type(row.get(f"{prefix}Phone{i} Type", ""))})
    return _dedupe_phones(out)


def parse_export(path: str | Path) -> list[DirectSkipRecord]:
    """Parse a downloaded portal ``contactinfo`` CSV into DirectSkipRecords.

    Uses the same required-header gate as the ``directskip-datasift-clean`` skill:
    a missing column means the layout drifted and every field would misalign.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []
    missing = [c for c in _CSV_REQUIRED if c not in rows[0]]
    if missing:
        raise DirectSkipError(
            f"Not a DirectSkip contactinfo file — missing {missing}. "
            "Do not proceed; columns would misalign.")

    records = []
    for row in rows:
        code = _clean(row.get("ResultCode")).upper()
        mf = _clean(row.get("Matched First Name")) or _clean(row.get("Input First Name"))
        ml = _clean(row.get("Matched Last Name")) or _clean(row.get("Input Last Name"))
        rec = DirectSkipRecord(
            input_name=f"{_clean(row.get('Input First Name'))} "
                       f"{_clean(row.get('Input Last Name'))}".strip(),
            first=mf, last=ml,
            property_address=_clean(row.get("Input Property Address")),
            property_city=_clean(row.get("Input Property City")),
            property_state=_clean(row.get("Input Property State")),
            property_zip=_clean(row.get("Input Property Zip")),
            mailing_address=_clean(row.get("Input Mailing Address")),
            mailing_city=_clean(row.get("Input Mailing City")),
            mailing_state=_clean(row.get("Input Mailing State")),
            mailing_zip=_clean(row.get("Input Mailing Zip")),
            result_code=code,
            deceased_flag=_truthy_deceased(row.get("Deceased")),
            address_only_match=code in ("AB1", "AB2"),
            subject_phones=_csv_phones(row, "", MAX_PHONES),
            confirmed_address=", ".join(v for v in [
                _clean(row.get("Confirmed Mailing Address")),
                _clean(row.get("Confirmed Mailing City")),
                _clean(row.get("Confirmed Mailing State")),
                _clean(row.get("Confirmed Mailing Zip"))] if v),
            source="csv",
        )
        for e in (_clean(row.get("Email1")).lower(), _clean(row.get("Email2")).lower()):
            if e:
                rec.subject_emails.append(e)

        # Additional people (Person2/Person3) + everyone's relatives → relatives.
        for pi in (2, 3):
            pre = f"Person{pi} "
            pn_first = _clean(row.get(pre + "First Name"))
            pn_last = _clean(row.get(pre + "Last Name"))
            if pn_first or pn_last:
                rec.relatives.append(_person_from(
                    pn_first, pn_last, "", row.get(pre + "Age"),
                    _csv_phones(row, pre, MAX_PHONES),
                    deceased=_truthy_deceased(row.get(pre + "Deceased"))))
        for pi in (0, 2, 3):
            pre = "" if pi == 0 else f"Person{pi} "
            for j in range(1, MAX_RELATIVES + 1):
                rname = _clean(row.get(f"{pre}Relative{j} Name"))
                if not rname:
                    continue
                if _absurd_age(row.get(f"{pre}Relative{j} Age", "")):
                    continue
                phones = []
                for k in range(1, 6):
                    num = _digits(row.get(f"{pre}Relative{j} Phone{k}", ""))
                    if num:
                        phones.append({"number": num,
                                       "type": _phone_type(row.get(f"{pre}Relative{j} Phone{k} Type", ""))})
                first, last = _split_relative_name(rname)
                rec.relatives.append(_person_from(
                    first, last, "", row.get(f"{pre}Relative{j} Age"),
                    _dedupe_phones(phones)))
        records.append(rec)

    logger.info("DirectSkip: parsed %d record(s) from %s, %d with results",
                len(records), path.name, sum(1 for r in records if r.has_results))
    return records


# ── Bridge into our heir map ──────────────────────────────────────────

def _status_for(person: DirectSkipPerson) -> str:
    """Map the vendor Deceased flag onto our heir status vocabulary.

    Never returns 'verified_living' — the vendor cannot prove someone is alive.
    A positive Deceased flag IS honoured (conservative: keeps them out of the
    signing chain). Mirrors smartskip._status_for.
    """
    return "deceased" if person.deceased_flag else "unverified"


def build_heir_map(record: DirectSkipRecord, executor_name: str = "") -> list[dict]:
    """Turn a DirectSkip record into our ``heir_map_json`` heir list.

    Signing authority comes from the ONE Texas intestacy implementation
    (``obituary_enricher.rank_decision_makers``); this only merges the
    DirectSkip-specific contact details onto the ranked entries.
    """
    from obituary_enricher import rank_decision_makers

    survivors = [{"name": p.name, "relationship": p.relationship}
                 for p in record.relatives if p.name]
    statuses = {p.name: _status_for(p) for p in record.relatives if p.name}

    ranked = rank_decision_makers(survivors, executor_name=executor_name,
                                  heir_statuses=statuses)

    by_name = {p.name.lower(): p for p in record.relatives if p.name}
    for entry in ranked:
        person = by_name.get(entry.get("name", "").lower())
        if not person:
            continue
        entry["source"] = "directskip"
        entry["phones"] = [p["number"] for p in person.phones]
        if person.street:
            entry["street"] = person.street
        if person.city:
            entry["city"] = person.city
        entry["state"] = person.state or "TX"
        if person.zip:
            entry["zip"] = person.zip
        if person.age:
            entry["age"] = person.age
        if person.relationship == "relative":
            entry["relationship_unconfirmed"] = True
    return ranked


_EMAIL_SLOTS = ["email_1", "email_2", "email_3", "email_4", "email_5"]


def apply_to_notice(notice, record: DirectSkipRecord) -> dict:
    """Write a DirectSkip result onto a NoticeData record. Returns a stats dict.

    Owner phones land in the flat dial slots; RELATIVES' phones stay inside
    ``heir_map_json`` so a relative's number is never dialled as the owner's.
    On an ADDRESS-ONLY match (AB1/AB2) the returned person is NOT the owner, so
    their phones never fill the owner slots — they remain in the heir map only.
    Deceased is recorded as an observation, never as ``owner_deceased``.
    """
    stats = {"subject_phones": 0, "relatives": 0, "relative_phones": 0, "heirs": 0}

    if record.deceased_flag and hasattr(notice, "smartskip_deceased_flag"):
        # Reuse the existing observation field (there is no directskip-specific one).
        notice.smartskip_deceased_flag = "yes"

    # ── Owner's own numbers → flat dial slots (skipped on an address-only match) ──
    if not record.address_only_match:
        existing = {getattr(notice, f, "") for f in _MOBILE_SLOTS + _LANDLINE_SLOTS}
        existing.discard("")
        mobiles = [p["number"] for p in record.subject_phones
                   if p.get("type") == "mobile" and p["number"] not in existing]
        others = [p["number"] for p in record.subject_phones
                  if p.get("type") != "mobile" and p["number"] not in existing]
        if not getattr(notice, "primary_phone", "") and (mobiles or others):
            notice.primary_phone = (mobiles or others)[0]
        for slot, number in zip(_MOBILE_SLOTS, mobiles):
            if not getattr(notice, slot, ""):
                setattr(notice, slot, number)
        for slot, number in zip(_LANDLINE_SLOTS, others):
            if not getattr(notice, slot, ""):
                setattr(notice, slot, number)
        stats["subject_phones"] = len(record.subject_phones)

        for slot, email in zip(_EMAIL_SLOTS, record.subject_emails):
            if hasattr(notice, slot) and not getattr(notice, slot, ""):
                setattr(notice, slot, email)

    # ── Relative graph → heir map ──
    heirs = build_heir_map(record, executor_name=getattr(notice, "decision_maker_name", "") or "")
    if heirs:
        notice.heir_map_json = json.dumps(heirs)
        stats["heirs"] = len(heirs)
        living = sum(1 for h in heirs if h.get("status") == "verified_living")
        deceased = sum(1 for h in heirs if h.get("status") == "deceased")
        notice.heirs_verified_living = str(living)
        notice.heirs_verified_deceased = str(deceased)

        # Promote a signer to DM #1 only if research has not already named one,
        # and never on an address-only match (the returned person isn't the owner).
        if not record.address_only_match and not getattr(notice, "decision_maker_name", ""):
            signer = next((h for h in heirs
                           if h.get("signing_authority") and h.get("status") != "deceased"), None)
            if signer:
                notice.decision_maker_name = signer["name"]
                notice.decision_maker_relationship = signer.get("relationship", "")
                notice.decision_maker_status = signer.get("status", "unverified")
                notice.dm_confidence = "low" if signer.get("relationship_unconfirmed") else "medium"
                notice.dm_confidence_reason = (
                    "DirectSkip relative graph; relationship unconfirmed by research"
                    if signer.get("relationship_unconfirmed")
                    else "DirectSkip relative graph; relationship pending research confirmation")

    stats["relatives"] = len(record.relatives)
    stats["relative_phones"] = sum(len(r.phones) for r in record.relatives)
    return stats


def apply_export(notices, export_path: str | Path) -> dict:
    """Match a downloaded portal export back onto NoticeData records by address."""
    records = parse_export(export_path)
    by_addr = {_addr_key(r.property_address): r for r in records if r.property_address}
    by_name = {f"{r.first} {r.last}".strip().lower(): r for r in records}

    totals = {"matched": 0, "unmatched": 0, "heirs": 0, "relative_phones": 0, "no_results": 0}
    for notice in notices:
        rec = by_addr.get(_addr_key(getattr(notice, "address", "")))
        if rec is None:
            rec = by_name.get((getattr(notice, "owner_name", "") or "").strip().lower())
        if rec is None:
            totals["unmatched"] += 1
            continue
        if not rec.has_results:
            totals["no_results"] += 1
        applied = apply_to_notice(notice, rec)
        totals["matched"] += 1
        totals["heirs"] += applied["heirs"]
        totals["relative_phones"] += applied["relative_phones"]

    logger.info("DirectSkip: applied export to %d record(s) — %d heirs, %d relative phone(s); "
                "%d unmatched, %d no results", totals["matched"], totals["heirs"],
                totals["relative_phones"], totals["unmatched"], totals["no_results"])
    return totals


def build_request_csv(notices, out_path: str | Path) -> tuple[Path, int, list]:
    """Write the portal upload CSV from NoticeData records (10-col, entity-filtered).

    Same layout SmartSkip uses; the portal wizard's step-2 selects map onto these
    headers. Entities are dropped up front (route to enformion_business.py).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["First Name", "Last Name", "Mailing Address", "Mailing City",
               "Mailing State", "Mailing Zip", "Property Address", "Property City",
               "Property State", "Property Zip"]
    written, skipped = 0, []
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for notice in notices:
            if getattr(notice, "business_name", "") or getattr(notice, "entity_type", ""):
                skipped.append(notice)
                continue
            first, last = _split_owner_name(getattr(notice, "owner_name", ""))
            if not (first and last):
                skipped.append(notice)
                continue
            writer.writerow({
                "First Name": first, "Last Name": last,
                "Mailing Address": getattr(notice, "mailing_address", "") or getattr(notice, "address", ""),
                "Mailing City": getattr(notice, "owner_city", "") or getattr(notice, "city", ""),
                "Mailing State": getattr(notice, "owner_state", "") or getattr(notice, "state", "TX"),
                "Mailing Zip": getattr(notice, "owner_zip", "") or getattr(notice, "zip", ""),
                "Property Address": getattr(notice, "address", ""),
                "Property City": getattr(notice, "city", ""),
                "Property State": getattr(notice, "state", "TX"),
                "Property Zip": getattr(notice, "zip", ""),
            })
            written += 1
    logger.info("DirectSkip: wrote %d row(s) to %s (%d skipped: entity/unusable name)",
                written, out_path, len(skipped))
    return out_path, written, skipped


def _record_summary(rec: DirectSkipRecord) -> dict:
    return {
        "input_name": rec.input_name, "matched": f"{rec.first} {rec.last}".strip(),
        "result_code": rec.result_code, "address_only_match": rec.address_only_match,
        "deceased_flag": rec.deceased_flag, "has_results": rec.has_results,
        "subject_phones": rec.subject_phones, "subject_emails": rec.subject_emails,
        "relatives": [{"name": p.name, "age": p.age,
                       "phones": [q["number"] for q in p.phones]} for p in rec.relatives],
    }


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["search", "batch", "parse", "balance",
                                        "list", "download"])
    parser.add_argument("--owner", help="owner name 'First Last' (search)")
    parser.add_argument("--property-address", default="")
    parser.add_argument("--property-city", default="")
    parser.add_argument("--property-state", default="TX")
    parser.add_argument("--property-zip", default="")
    parser.add_argument("--mailing-address", default="")
    parser.add_argument("--mailing-city", default="")
    parser.add_argument("--mailing-state", default="TX")
    parser.add_argument("--mailing-zip", default="")
    parser.add_argument("--dnc-scrub", action="store_true", help="add DNC/litigator scrub")
    parser.add_argument("--input", help="CSV of records (batch / build-csv)")
    parser.add_argument("--max-cost", type=float, help="hard USD cost cap for batch")
    parser.add_argument("--export", help="a result JSON or portal contactinfo CSV to parse")
    parser.add_argument("--id", dest="order_id", help="portal order id or download code")
    parser.add_argument("--out", help="output path (download)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    if args.cmd == "parse":
        if not args.export:
            parser.error("--export required")
        p = Path(args.export)
        if p.suffix.lower() == ".json":
            rec = parse_api_response(json.loads(p.read_text(encoding="utf-8")))
            print(json.dumps(_record_summary(rec), indent=2))
        else:
            print(json.dumps([_record_summary(r) for r in parse_export(p)], indent=2))
        return 0

    client = DirectSkipClient()

    if args.cmd == "balance":
        print(json.dumps(client.balance(), indent=2))
        return 0

    if args.cmd == "list":
        for o in client.list_orders():
            print(json.dumps(o))
        return 0

    if args.cmd == "download":
        if not (args.order_id and args.out):
            parser.error("--id and --out required")
        client.download(args.order_id, args.out)
        return 0

    if args.cmd == "search":
        if not (args.owner and args.property_address):
            parser.error("--owner and --property-address required")
        first, last = _split_owner_name(args.owner)
        if not (first and last):
            parser.error(f"could not split owner name {args.owner!r} into first/last")
        data = client.search_contact(
            first=first, last=last,
            mailing_address=args.mailing_address or args.property_address,
            mailing_city=args.mailing_city or args.property_city,
            mailing_state=args.mailing_state, mailing_zip=args.mailing_zip or args.property_zip,
            property_address=args.property_address, property_city=args.property_city,
            property_state=args.property_state, property_zip=args.property_zip,
            dnc_scrub=args.dnc_scrub)
        rec = parse_api_response(data)
        print(json.dumps(_record_summary(rec), indent=2))
        print(f"\n{'MATCH' if rec.has_results else 'NO MATCH (no charge)'} — "
              f"result_code={rec.result_code or 'none'}", file=sys.stderr)
        return 0

    if args.cmd == "batch":
        if not args.input:
            parser.error("--input required")
        parser.error("batch requires a list of NoticeData objects; call "
                     "DirectSkipClient.batch_search(notices, max_cost=...) from the pipeline. "
                     "The CLI 'batch' over a raw CSV is not wired yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

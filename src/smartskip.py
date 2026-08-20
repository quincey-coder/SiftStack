"""SmartSkip bulk skip-trace client + export parser — the Deep Prospecting v5 heir engine.

SmartSkip (app.smartskip.io) returns an owner's RELATIVES **with their phone
numbers** in one batch row. That is the piece our stack never had: until now the
heir set was extracted from obituary prose by an LLM, which is the job that
hallucinated a family in the Norman Willis incident (see the grounding guard in
``obituary_enricher``). SmartSkip hands over a grounded relative graph, so the
research layer only has to *confirm* relationships and supply the date of death —
a far safer job than inventing the list.

It replaces the Enformion/Endato **Person** search (never wired into this fork;
measured upstream at zero relatives on 6 of 12 owners, and its relatives carry no
phones). Enformion **BusinessV2** is retained separately for entity owners, which
SmartSkip cannot trace at all (it needs a first and last name).

Stack position:
    SmartSkip (relatives + phones)  ->  DirectSkip (gap-fill the ~7% phoneless)
      ->  obituary/web research (MANDATORY: date of death + true relationships)
      ->  Trestle (dial tiers)

MONEY SAFETY — read before using:
  * ``submit`` / ``status`` / ``download`` are FREE.
  * ``pay`` BILLS THE SAVED CARD and is the only billing call. It is never
    invoked implicitly by any pipeline path, and it refuses to run unless the
    caller passes ``confirm_rows`` matching the row count SmartSkip calculated —
    so a mis-scoped batch cannot be paid for by accident.
  * The SmartSkip *wallet* does NOT fund bulk skip; bulk always charges the card.
  * An UNPAID order is invisible in ``GET /bulk-skip``. The bulkSkipId is
    persisted to the run dir on submit so an unpaid order is never lost.

Trust boundaries (all verified upstream against the live account, 2026-07-29):
  * ``Deceased`` is unreliable and there is NO date-of-death column. A false
    ``Deceased=false`` was returned for a man with a published obituary. Death is
    decided by the research layer, never here — see ``_status_for``.
  * Relationship labels are a coarse "Possible Type"; ~63% come back generic.
    Generic labels map to "relative", which carries NO signing authority until
    research upgrades it.

CLI:
    python src/smartskip.py submit   --csv owners.csv
    python src/smartskip.py pay      --id <bulkSkipId> --confirm-rows <n>   # BILLS
    python src/smartskip.py status   [--id <bulkSkipId>]
    python src/smartskip.py download --id <bulkSkipId> --out export.csv
    python src/smartskip.py parse    --export export.csv
    python src/smartskip.py balance
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import requests

import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.smartskip.io"
REQUEST_TIMEOUT = 120

# Cost model (upstream-measured, 2026-07-29). Used only for estimates/logging.
COST_PER_HIT = 0.15

# SmartSkip caps relatives at 50 per property in the wide export, 18 phones per
# person in the tall export, 5 phones per relative in the wide one.
MAX_RELATIVES = 50
MAX_PHONES_TALL = 18
MAX_PHONES_WIDE = 5

# An age above this is bad provider data, not a person.
MAX_PLAUSIBLE_AGE = 100

_RUN_DIR = Path(getattr(config, "PROJECT_ROOT", Path.cwd())) / "data" / "smartskip"
SESSION_FILE = _RUN_DIR / "session.json"
ORDERS_FILE = _RUN_DIR / "orders.json"


class SmartSkipError(RuntimeError):
    """Any SmartSkip API failure. Raised loudly — never swallowed into a zero result."""


# ── CSV header synonyms → SmartSkip api field ─────────────────────────
# Normalized (lowercase alphanumerics only) so "Owner First Name" == "ownerfirstname".
SYNONYMS = {
    "firstName": ["firstname", "ownerfirstname", "first", "fname"],
    "lastName": ["lastname", "ownerlastname", "last", "lname"],
    "middleName": ["middlename", "middle", "mi"],
    "mailingAddress": ["mailingaddress", "mailingstreet", "mailingaddr",
                       "owneraddress", "mailaddress"],
    "mailingCity": ["mailingcity", "mailcity"],
    "mailingState": ["mailingstate", "mailstate", "mailingst"],
    "mailingZip": ["mailingzip", "mailingzip5", "mailingzipcode", "mailzip"],
    "propertyAddress": ["propertyaddress", "propertystreet", "propertyaddr",
                        "address", "situsaddress", "street"],
    "propertyCity": ["propertycity", "city", "situscity"],
    "propertyState": ["propertystate", "state", "situsstate"],
    "propertyZip": ["propertyzip", "propertyzip5", "propertyzipcode", "zip", "situszip"],
}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


# ── Client ────────────────────────────────────────────────────────────

class SmartSkipClient:
    """Authenticated SmartSkip session with token caching and auto-refresh.

    Access tokens live ~15 minutes and refresh tokens ~30 days, so a long batch
    poll would die mid-run without the refresh path.
    """

    def __init__(self, email: str = "", password: str = "", run_dir: Path | None = None):
        self.email = email or getattr(config, "SMARTSKIP_EMAIL", "")
        self.password = password or getattr(config, "SMARTSKIP_PASSWORD", "")
        self.run_dir = Path(run_dir) if run_dir else _RUN_DIR
        self.session_file = self.run_dir / "session.json"
        self.orders_file = self.run_dir / "orders.json"
        self._tokens: dict = {}
        if self.session_file.exists():
            try:
                self._tokens = json.loads(self.session_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._tokens = {}

    # ── auth ──
    def _save_tokens(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps(self._tokens), encoding="utf-8")

    def _signin(self) -> None:
        if not self.email or not self.password:
            raise SmartSkipError(
                "No SmartSkip credentials. Set SMARTSKIP_EMAIL / SMARTSKIP_PASSWORD in .env"
            )
        resp = requests.post(f"{API_BASE}/auth/signin",
                             json={"email": self.email, "password": self.password},
                             timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise SmartSkipError(f"signin failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        self._tokens = {"accessToken": data["accessToken"],
                        "refreshToken": data["refreshToken"]}
        self._save_tokens()
        logger.info("SmartSkip: signed in as %s", self.email)

    def _refresh(self) -> bool:
        rt = self._tokens.get("refreshToken")
        if not rt:
            return False
        resp = requests.get(f"{API_BASE}/auth/refresh",
                            headers={"Authorization": f"Bearer {rt}"},
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("accessToken"):
                self._tokens = {"accessToken": data["accessToken"],
                                "refreshToken": data.get("refreshToken", rt)}
                self._save_tokens()
                return True
        return False

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        if not self._tokens.get("accessToken") and not self._refresh():
            self._signin()
        url = path if path.startswith("http") else API_BASE + path
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._tokens['accessToken']}"
        resp = requests.request(method, url, headers=headers,
                                timeout=REQUEST_TIMEOUT, **kwargs)
        if resp.status_code in (401, 403):
            if not self._refresh():
                self._signin()
            headers["Authorization"] = f"Bearer {self._tokens['accessToken']}"
            resp = requests.request(method, url, headers=headers,
                                    timeout=REQUEST_TIMEOUT, **kwargs)
        return resp

    # ── order bookkeeping ──
    def _load_orders(self) -> list[dict]:
        if self.orders_file.exists():
            try:
                return json.loads(self.orders_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        return []

    def _save_order(self, entry: dict) -> None:
        orders = [o for o in self._load_orders()
                  if o.get("bulkSkipId") != entry["bulkSkipId"]] + [entry]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.orders_file.write_text(json.dumps(orders, indent=2), encoding="utf-8")

    # ── lifecycle (all free except pay) ──
    def upload(self, csv_path: str | Path, tries: int = 3) -> dict:
        """Step 1 — upload the CSV. Returns {bulkSkipId, parsed}.

        Retried: on 2026-08-20 the API returned a transient 400 "File is empty"
        for a well-formed 1-row CSV that uploaded fine minutes later (verified
        by re-uploading the byte-identical file). Upload is FREE and an unpaid
        bulkSkipId is inert server-side, so retrying costs nothing.
        """
        csv_path = Path(csv_path)
        last = ""
        for attempt in range(tries):
            if attempt:
                time.sleep(3 * attempt)
            with csv_path.open("rb") as fh:
                resp = self._call("POST", "/bulk-skip/mapping",
                                  files={"file": (csv_path.name, fh, "text/csv")})
            if resp.status_code in (200, 201):
                return resp.json()
            last = f"upload failed ({resp.status_code}): {resp.text[:300]}"
            logger.warning("SmartSkip %s (attempt %d/%d)", last, attempt + 1, tries)
        raise SmartSkipError(last)

    def fields(self) -> dict:
        """Step 2 — the required/optional field catalogue."""
        resp = self._call("GET", "/bulk-skip/fields")
        if resp.status_code != 200:
            raise SmartSkipError(f"fields fetch failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def auto_map(self, headers: list[str], overrides: dict | None = None) -> dict:
        """Match our CSV headers to SmartSkip api fields, case/punctuation-blind."""
        overrides = overrides or {}
        catalogue = self.fields()
        api_fields = dict(catalogue.get("required", {}))
        api_fields.update(catalogue.get("optional", {}))
        by_norm = {_norm(h): h for h in headers}

        schema: dict[str, str] = {}
        for api_field, label in api_fields.items():
            if api_field in overrides:
                if overrides[api_field] not in headers:
                    raise SmartSkipError(
                        f"--map {api_field}={overrides[api_field]!r}: no such CSV column"
                    )
                schema[api_field] = overrides[api_field]
                continue
            for cand in [_norm(label), _norm(api_field)] + SYNONYMS.get(api_field, []):
                if cand in by_norm:
                    schema[api_field] = by_norm[cand]
                    break

        missing = [f for f in catalogue.get("required", {}) if f not in schema]
        if missing:
            raise SmartSkipError(
                f"Could not map required field(s) {missing} to CSV headers {list(headers)}. "
                f"Use --map \"field=CSV Header\"."
            )
        return schema

    def submit(self, csv_path: str | Path, overrides: dict | None = None) -> dict:
        """FREE: upload + map + calculate. Returns the order entry, nothing billed."""
        csv_path = Path(csv_path)
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            headers = next(csv.reader(fh))

        uploaded = self.upload(csv_path)
        bulk_id = uploaded["bulkSkipId"]
        schema = self.auto_map(headers, overrides)

        resp = self._call("POST", f"/bulk-skip/fields/{bulk_id}", json={"schema": schema})
        if resp.status_code not in (200, 201):
            raise SmartSkipError(f"field mapping failed ({resp.status_code}): {resp.text[:300]}")

        resp = self._call("POST", f"/bulk-skip/calculate/{bulk_id}")
        if resp.status_code not in (200, 201):
            raise SmartSkipError(f"calculate failed ({resp.status_code}): {resp.text[:300]}")
        calc = resp.json()

        entry = {
            "bulkSkipId": bulk_id,
            "file": csv_path.name,
            "schema": schema,
            "entities": calc.get("entities"),
            "duplicates": calc.get("duplicates"),
            "status": calc.get("status"),
            "paid": False,
        }
        # Persisted BEFORE any payment: an unpaid order does not appear in the
        # order list, so losing this id would orphan the upload entirely.
        self._save_order(entry)
        rows = calc.get("entities") or 0
        logger.info("SmartSkip: order %s calculated — %s billable row(s), est $%.2f. "
                    "NOT PAID (nothing billed).", bulk_id, rows, rows * COST_PER_HIT)
        return entry

    def pay(self, bulk_id: str, confirm_rows: int, payment_method_id: str = "") -> bool:
        """BILLS THE SAVED CARD. Requires confirm_rows to match the calculated count.

        The confirmation guard exists because this is the one irreversible call in
        the module: a mis-scoped CSV would otherwise silently bill for thousands of
        rows. The caller must state the row count it believes it is paying for.
        """
        order = next((o for o in self._load_orders() if o.get("bulkSkipId") == bulk_id), None)
        if order is None:
            raise SmartSkipError(
                f"Unknown bulkSkipId {bulk_id} — not in {self.orders_file}. "
                f"Refusing to pay for an order this client did not submit."
            )
        calculated = int(order.get("entities") or 0)
        if int(confirm_rows) != calculated:
            raise SmartSkipError(
                f"Refusing to pay: confirm_rows={confirm_rows} but SmartSkip calculated "
                f"{calculated} billable row(s) for {bulk_id}. Re-check the batch scope."
            )

        if not payment_method_id:
            resp = self._call("GET", "/payment/payment-method")
            methods = resp.json() if resp.status_code == 200 else []
            if not methods:
                raise SmartSkipError(f"No saved payment method ({resp.status_code})")
            method = next((m for m in methods if m.get("isDefault")), methods[0])
            payment_method_id = method["id"]
            logger.info("SmartSkip: charging %s ...%s (default card) for %d row(s), est $%.2f",
                        method.get("brand"), method.get("last4"), calculated,
                        calculated * COST_PER_HIT)

        resp = self._call("POST", "/bulk-skip/payment-intent",
                          json={"bulkSkipId": bulk_id, "paymentMethodId": payment_method_id})
        if resp.status_code not in (200, 201):
            raise SmartSkipError(f"payment-intent failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()

        if data.get("clientSecret"):
            logger.error("SmartSkip: payment needs 3DS confirmation — complete it once at "
                         "https://app.smartskip.io/bulk-skip, then re-run status/download.")
            return False

        status = data.get("status")
        ok = status in ("succeeded", "processing", "requires_capture")
        if ok:
            order["paid"] = True
            self._save_order(order)
            logger.info("SmartSkip: payment %s (intent %s)", status, data.get("paymentIntentId"))
        else:
            logger.error("SmartSkip: payment status=%s — card likely declined", status)
        return ok

    def status(self, bulk_id: str = "") -> list[dict]:
        """Order status. NOTE: unpaid orders never appear here."""
        resp = self._call("GET", "/bulk-skip?sortField=createdAt&sortOrder=desc")
        if resp.status_code != 200:
            raise SmartSkipError(f"status fetch failed ({resp.status_code}): {resp.text[:300]}")
        items = resp.json().get("items", [])
        return [i for i in items if i.get("_id") == bulk_id] if bulk_id else items

    def download(self, bulk_id: str, out_path: str | Path, fmt: str = "vertical") -> Path:
        """Pull the finished export. vertical=Campaign (tall), horizontal=CRM (wide)."""
        resp = self._call("GET", f"/bulk-skip/download/{bulk_id}?type={fmt}")
        if resp.status_code != 200:
            raise SmartSkipError(f"download failed ({resp.status_code}): {resp.text[:300]}")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        logger.info("SmartSkip: saved %s (%d bytes)", out_path, len(resp.content))
        return out_path

    def balance(self) -> dict:
        """Wallet balance + saved cards. The wallet does NOT fund bulk skip."""
        wallet = self._call("GET", "/wallet/balance")
        cards = self._call("GET", "/payment/payment-method")
        return {
            "wallet": wallet.json() if wallet.status_code == 200 else None,
            "payment_methods": cards.json() if cards.status_code == 200 else None,
        }

    def wait_for_completion(self, bulk_id: str, max_seconds: int = 900,
                            poll_seconds: int = 15) -> str:
        """Poll a PAID order to completion. Returns the final status."""
        deadline = time.time() + max_seconds
        while time.time() < deadline:
            items = self.status(bulk_id)
            state = (items[0].get("status") if items else "") or "unknown"
            logger.info("SmartSkip: order %s status=%s", bulk_id, state)
            if state.lower() == "completed":
                return state
            if state.lower() in ("error", "failed"):
                raise SmartSkipError(f"bulk skip ended in status {state}")
            time.sleep(poll_seconds)
        return "timeout"


# ── Export parsing ────────────────────────────────────────────────────

@dataclass
class SmartSkipPerson:
    """One person (subject or relative) from a SmartSkip export."""
    first: str = ""
    last: str = ""
    name: str = ""
    relationship_raw: str = ""      # SmartSkip "Possible Type" — coarse, ~63% generic
    relationship: str = "relative"  # canonical, lowercase, feeds rank_decision_makers
    age: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phones: list[dict] = field(default_factory=list)   # [{number, type}]
    deceased_flag: bool = False     # SmartSkip's flag — UNRELIABLE, see module docstring


@dataclass
class SmartSkipRecord:
    """One traced owner plus their relative graph."""
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
    deceased_flag: bool = False
    subject_phones: list[dict] = field(default_factory=list)
    relatives: list[SmartSkipPerson] = field(default_factory=list)
    associate_keys: list[list[str]] = field(default_factory=list)
    export_format: int = 0

    @property
    def has_results(self) -> bool:
        return bool(self.subject_phones) or any(r.phones for r in self.relatives)


def _digits(raw: str) -> str:
    """Normalize to a bare 10-digit US number, or '' if it isn't one."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def _absurd_age(raw) -> bool:
    try:
        return int(str(raw).strip()) > MAX_PLAUSIBLE_AGE
    except (ValueError, TypeError):
        return False


def _truthy(raw) -> bool:
    return str(raw or "").strip().lower() in ("true", "y", "yes", "1")


def canon_relationship(possible_type: str) -> str:
    """Map SmartSkip's coarse 'Possible Type' to a term rank_decision_makers knows.

    Deliberately gender-neutral: SmartSkip gives no gender and guessing one from a
    first name would put a fabricated attribute on a real person. The neutral terms
    ('child', 'parent', 'sibling', 'spouse') are already recognised by the TX
    intestacy classifier, so nothing is lost by not guessing.

    Anything unknown becomes 'relative', which carries NO signing authority — the
    safe default given ~63% of labels come back generic.
    """
    t = (possible_type or "").strip().lower()
    if t == "child":
        return "child"
    if t == "parent":
        return "parent"
    if t == "sibling":
        return "sibling"
    if t == "spouse":
        return "spouse"
    if t in ("in-law", "in law", "inlaw"):
        return "in-law"
    return "relative"


def _name_key(first: str, last: str) -> list[str] | None:
    toks = [t for t in re.split(r"[ .]+", f"{first or ''} {last or ''}".upper()) if len(t) > 1]
    return [toks[0], toks[-1]] if len(toks) >= 2 else None


def _dedupe_phones(phones: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for p in phones:
        if p["number"] in seen:
            continue
        seen.add(p["number"])
        out.append(p)
    return out


def _phones_tall(row: dict, count: int = MAX_PHONES_TALL) -> list[dict]:
    out = []
    for i in range(1, count + 1):
        num = _digits(row.get(f"Phone {i} number", ""))
        if num:
            out.append({"number": num, "type": (row.get(f"Phone {i} type", "") or "").strip()})
    return _dedupe_phones(out)


def _detect_format(header) -> int:
    """1 = vertical/Campaign (row per person), 2 = horizontal/CRM (row per property)."""
    header = list(header)
    if "Input Name" in header:
        return 1
    if any(h.startswith("RELATIVE 1: First Name") for h in header):
        return 2
    raise SmartSkipError(
        "Unrecognized SmartSkip export (no 'Input Name' and no 'RELATIVE 1:' columns)"
    )


def _person_from(first, last, ptype, age, street, city, state, zip_code,
                 phones, deceased=False) -> SmartSkipPerson:
    return SmartSkipPerson(
        first=(first or "").strip(),
        last=(last or "").strip(),
        name=f"{(first or '').strip()} {(last or '').strip()}".strip(),
        relationship_raw=(ptype or "").strip(),
        relationship=canon_relationship(ptype),
        age=str(age or "").strip(),
        street=(street or "").strip(),
        city=(city or "").strip(),
        state=(state or "").strip(),
        zip=(zip_code or "").strip(),
        phones=phones,
        deceased_flag=deceased,
    )


def parse_export(path: str | Path) -> list[SmartSkipRecord]:
    """Read EITHER SmartSkip export format into one normalized shape.

    Keeps Subject + Relatives, DROPS Associates (non-family; they carry no
    inheritance interest and calling them is a privacy problem, not a lead).
    Their name keys are retained so a downstream pass can exclude them.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []

    fmt = _detect_format(rows[0].keys())
    records: list[SmartSkipRecord] = []

    if fmt == 1:
        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for row in rows:
            groups.setdefault(row.get("Input Name", ""), []).append(row)

        for input_name, group in groups.items():
            subject = next((r for r in group if r.get("Relationship") == "Subject"), group[0])
            rec = SmartSkipRecord(
                input_name=input_name,
                first=(subject.get("First Name") or "").strip(),
                last=(subject.get("Last Name") or "").strip(),
                property_address=(subject.get("Property Address") or "").strip(),
                property_city=(subject.get("Property City") or "").strip(),
                property_state=(subject.get("Property State") or "").strip(),
                property_zip=(subject.get("Property Zip") or "").strip(),
                mailing_address=(subject.get("Mailing Address") or "").strip(),
                mailing_city=(subject.get("Mailing City") or "").strip(),
                mailing_state=(subject.get("Mailing State") or "").strip(),
                mailing_zip=(subject.get("Mailing Zip") or "").strip(),
                deceased_flag=_truthy(subject.get("Deceased")),
                subject_phones=_phones_tall(subject),
                export_format=fmt,
            )
            for row in group:
                rel = row.get("Relationship")
                if rel == "Associate":
                    key = _name_key(row.get("First Name"), row.get("Last Name"))
                    if key:
                        rec.associate_keys.append(key)
                    continue
                if rel != "Relative" or _absurd_age(row.get("Age", "")):
                    continue
                rec.relatives.append(_person_from(
                    row.get("First Name"), row.get("Last Name"), row.get("Possible Type"),
                    row.get("Age"), row.get("Mailing Address"), row.get("Mailing City"),
                    row.get("Mailing State"), row.get("Mailing Zip"),
                    _phones_tall(row), _truthy(row.get("Deceased")),
                ))
            records.append(rec)
    else:
        for row in rows:
            rec = SmartSkipRecord(
                input_name=f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip(),
                first=(row.get("First Name") or "").strip(),
                last=(row.get("Last Name") or "").strip(),
                property_address=(row.get("Property Address") or "").strip(),
                property_city=(row.get("Property City") or "").strip(),
                property_state=(row.get("Property State") or "").strip(),
                property_zip=(row.get("Property Zip") or "").strip(),
                mailing_address=(row.get("Mailing Address") or "").strip(),
                mailing_city=(row.get("Mailing City") or "").strip(),
                mailing_state=(row.get("Mailing State") or "").strip(),
                mailing_zip=(row.get("Mailing Zip") or "").strip(),
                deceased_flag=_truthy(row.get("Deceased")),
                subject_phones=_phones_tall(row),
                export_format=fmt,
            )
            for i in range(1, MAX_RELATIVES + 1):
                first = row.get(f"RELATIVE {i}: First Name", "")
                if not (first and first.strip()):
                    continue
                if _absurd_age(row.get(f"RELATIVE {i}: Age", "")):
                    continue
                phones = []
                for j in range(1, MAX_PHONES_WIDE + 1):
                    num = _digits(row.get(f"RELATIVE {i}: Phone {j} number", ""))
                    if num:
                        phones.append({
                            "number": num,
                            "type": (row.get(f"RELATIVE {i}: Phone {j} type", "") or "").strip(),
                        })
                rec.relatives.append(_person_from(
                    first, row.get(f"RELATIVE {i}: Last Name"),
                    row.get(f"RELATIVE {i}: Possible Type"), row.get(f"RELATIVE {i}: Age"),
                    row.get(f"RELATIVE {i}: Mailing Street"), row.get(f"RELATIVE {i}: Mailing City"),
                    row.get(f"RELATIVE {i}: Mailing State"), row.get(f"RELATIVE {i}: Mailing ZIP Code"),
                    _dedupe_phones(phones),
                ))
            for i in range(1, 6):
                first = row.get(f"ASSOCIATE {i}: First Name", "")
                if first and first.strip():
                    key = _name_key(first, row.get(f"ASSOCIATE {i}: Last Name"))
                    if key:
                        rec.associate_keys.append(key)
            records.append(rec)

    logger.info("SmartSkip: parsed %d record(s) from %s (format %d), %d with results",
                len(records), path.name, fmt, sum(1 for r in records if r.has_results))
    return records


# ── Bridge into our heir map ──────────────────────────────────────────

def _status_for(person: SmartSkipPerson) -> str:
    """Map SmartSkip's Deceased flag onto our heir status vocabulary.

    Never returns 'verified_living'. SmartSkip cannot verify that someone is
    alive — it returned Deceased=false for a man with a published obituary — so
    the most it can support is 'unverified', which the research layer upgrades.
    A positive Deceased flag IS honoured: the flag under-reports death, so a
    'true' is credible, and treating it as deceased keeps that person out of the
    signing chain (the conservative direction in both cases).
    """
    return "deceased" if person.deceased_flag else "unverified"


def build_heir_map(record: SmartSkipRecord, executor_name: str = "") -> list[dict]:
    """Turn a SmartSkip record into our ``heir_map_json`` heir list.

    Reuses ``obituary_enricher.rank_decision_makers`` so signing authority comes
    from the ONE Texas intestacy implementation rather than a second copy that
    could drift. Contact details (phones/address) that only SmartSkip has are
    merged back onto the ranked entries afterwards.
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
        entry["source"] = "smartskip"
        entry["phones"] = [p["number"] for p in person.phones]
        entry["street"] = person.street
        entry["city"] = person.city
        entry["state"] = person.state or "TX"
        entry["zip"] = person.zip
        if person.age:
            entry["age"] = person.age
        # Keep the provider's own wording so research can see what it is overriding.
        if person.relationship_raw:
            entry["relationship_raw"] = person.relationship_raw
        # ~63% of labels are generic; mark those so the research layer knows the
        # relationship (and therefore the signing analysis) is still a hypothesis.
        if person.relationship == "relative":
            entry["relationship_unconfirmed"] = True
    return ranked


# Flat DM phone slots on NoticeData, in dial order. Mirrors
# phone_validator.DM_PHONE_FIELDS so Trestle scores everything we write.
_MOBILE_SLOTS = ["mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5"]
_LANDLINE_SLOTS = ["landline_1", "landline_2", "landline_3"]


def apply_to_notice(notice, record: SmartSkipRecord) -> dict:
    """Write a SmartSkip result onto a NoticeData record. Returns a stats dict.

    Deliberately does NOT set ``owner_deceased`` or ``date_of_death``. SmartSkip
    has no date-of-death column and its Deceased flag returned false for a man
    with a published obituary, so death stays the research layer's call — this
    only records what SmartSkip actually observed, in ``smartskip_deceased_flag``.

    Owner phones land in the flat dial slots; RELATIVES' phones stay inside
    ``heir_map_json`` so a relative's number is never dialled as if it were the
    owner's. On a shared household line the owner rule wins.
    """
    stats = {"subject_phones": 0, "relatives": 0, "relative_phones": 0, "heirs": 0}

    # ── Owner's own numbers → flat dial slots ──
    existing = {getattr(notice, f, "") for f in _MOBILE_SLOTS + _LANDLINE_SLOTS}
    existing.discard("")
    mobiles = [p["number"] for p in record.subject_phones
               if "mobile" in (p.get("type") or "").lower() and p["number"] not in existing]
    others = [p["number"] for p in record.subject_phones
              if "mobile" not in (p.get("type") or "").lower() and p["number"] not in existing]

    if not getattr(notice, "primary_phone", "") and (mobiles or others):
        notice.primary_phone = (mobiles or others)[0]
    for slot, number in zip(_MOBILE_SLOTS, mobiles):
        if not getattr(notice, slot, ""):
            setattr(notice, slot, number)
    for slot, number in zip(_LANDLINE_SLOTS, others):
        if not getattr(notice, slot, ""):
            setattr(notice, slot, number)
    stats["subject_phones"] = len(record.subject_phones)

    # SmartSkip's own read on the owner, kept as an observation, never a verdict.
    if record.deceased_flag and hasattr(notice, "smartskip_deceased_flag"):
        notice.smartskip_deceased_flag = "yes"

    # ── Relative graph → heir map ──
    heirs = build_heir_map(record, executor_name=getattr(notice, "decision_maker_name", "") or "")
    if heirs:
        notice.heir_map_json = json.dumps(heirs)
        stats["heirs"] = len(heirs)
        living = sum(1 for h in heirs if h.get("status") == "verified_living")
        deceased = sum(1 for h in heirs if h.get("status") == "deceased")
        notice.heirs_verified_living = str(living)
        notice.heirs_verified_deceased = str(deceased)

        # Top-ranked signer becomes DM #1 only if research has not already named
        # one. A court-named PR/executor outranks anything a provider infers.
        if not getattr(notice, "decision_maker_name", ""):
            signer = next((h for h in heirs
                           if h.get("signing_authority") and h.get("status") != "deceased"), None)
            if signer:
                notice.decision_maker_name = signer["name"]
                notice.decision_maker_relationship = signer.get("relationship", "")
                notice.decision_maker_status = signer.get("status", "unverified")
                # Never "high": the relationship behind it is a coarse provider
                # label until the obituary/web pass confirms it.
                notice.dm_confidence = "low" if signer.get("relationship_unconfirmed") else "medium"
                notice.dm_confidence_reason = (
                    "SmartSkip relative graph; relationship unconfirmed by research"
                    if signer.get("relationship_unconfirmed")
                    else "SmartSkip relative graph; relationship pending research confirmation"
                )

    stats["relatives"] = len(record.relatives)
    stats["relative_phones"] = sum(len(r.phones) for r in record.relatives)
    return stats


def apply_export(notices, export_path: str | Path) -> dict:
    """Match a downloaded SmartSkip export back onto NoticeData records.

    Matching is by property address first (the stable key), falling back to
    owner first+last. Returns aggregate stats.
    """
    records = parse_export(export_path)
    by_addr = {_addr_key(r.property_address): r for r in records if r.property_address}
    by_name = {f"{r.first} {r.last}".strip().lower(): r for r in records}

    totals = {"matched": 0, "unmatched": 0, "heirs": 0, "relative_phones": 0,
              "no_results": 0}
    for notice in notices:
        rec = by_addr.get(_addr_key(getattr(notice, "address", "")))
        if rec is None:
            rec = by_name.get((getattr(notice, "owner_name", "") or "").strip().lower())
        if rec is None:
            totals["unmatched"] += 1
            continue
        if not rec.has_results:
            totals["no_results"] += 1
        stats = apply_to_notice(notice, rec)
        totals["matched"] += 1
        totals["heirs"] += stats["heirs"]
        totals["relative_phones"] += stats["relative_phones"]

    logger.info("SmartSkip: applied export to %d record(s) — %d heirs, %d relative phone(s); "
                "%d unmatched, %d returned no results",
                totals["matched"], totals["heirs"], totals["relative_phones"],
                totals["unmatched"], totals["no_results"])
    return totals


def _addr_key(address: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (address or "").lower())


def build_trace_csv(notices, out_path: str | Path) -> tuple[Path, int, list]:
    """Write the SmartSkip input CSV from NoticeData records.

    Entities are filtered OUT up front, not discovered per-record: SmartSkip needs
    a first and last name, so LLCs/trusts/estates return nothing and would be
    billed as misses. Those route to Enformion BusinessV2 instead. Returns
    (path, rows_written, skipped_entities).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    columns = ["First Name", "Last Name", "Mailing Address", "Mailing City",
               "Mailing State", "Mailing Zip", "Property Address", "Property City",
               "Property State", "Property Zip"]

    written = 0
    skipped: list = []
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for notice in notices:
            # business_name is set by the formatter for anything classified as an
            # entity; entity_type covers llc/corp/trust/estate/lp.
            if getattr(notice, "business_name", "") or getattr(notice, "entity_type", ""):
                skipped.append(notice)
                continue
            first, last = _split_owner_name(getattr(notice, "owner_name", ""))
            if not (first and last):
                skipped.append(notice)
                continue
            writer.writerow({
                "First Name": first,
                "Last Name": last,
                # mailing_address is a street only; fall back to the property line
                # so the required mailingAddress field is never blank.
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

    logger.info("SmartSkip: wrote %d row(s) to %s (%d skipped: entity or unusable name), est $%.2f",
                written, out_path, len(skipped), written * COST_PER_HIT)
    return out_path, written, skipped


def _split_owner_name(owner_name: str) -> tuple[str, str]:
    """Split ``owner_name`` into (first, last).

    ``owner_name`` is FIRST [MIDDLE] LAST by contract across this codebase — the
    raw LAST-FIRST (NAMELF) form lives in ``tax_owner_name`` and must never be
    passed here. See the Owner-Name Orientation rules in CLAUDE.md.
    """
    parts = [p for p in re.split(r"\s+", (owner_name or "").strip()) if p]
    if len(parts) < 2:
        return ("", "")
    return (parts[0], parts[-1])


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["submit", "pay", "status", "download",
                                        "parse", "balance"])
    parser.add_argument("--csv", help="input CSV (submit)")
    parser.add_argument("--id", dest="bulk_id", help="bulkSkipId")
    parser.add_argument("--out", help="output path (download)")
    parser.add_argument("--export", help="downloaded export to parse")
    parser.add_argument("--format", default="vertical", choices=["vertical", "horizontal"])
    parser.add_argument("--map", action="append", default=[],
                        help='override mapping: "apiField=CSV Header"')
    parser.add_argument("--confirm-rows", type=int,
                        help="REQUIRED for pay: the billable row count you expect")
    parser.add_argument("--pm", help="Stripe paymentMethodId (default: account default card)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    if args.cmd == "parse":
        if not args.export:
            parser.error("--export required")
        records = parse_export(args.export)
        print(json.dumps([{
            "input_name": r.input_name, "has_results": r.has_results,
            "deceased_flag": r.deceased_flag,
            "subject_phones": r.subject_phones,
            "relatives": [vars(p) for p in r.relatives],
        } for r in records], indent=2))
        return 0

    client = SmartSkipClient()

    if args.cmd == "balance":
        print(json.dumps(client.balance(), indent=2))
        return 0

    if args.cmd == "submit":
        if not args.csv:
            parser.error("--csv required")
        entry = client.submit(args.csv, dict(m.split("=", 1) for m in args.map))
        print(json.dumps(entry, indent=2))
        print(f"\nNOTHING BILLED. To charge the card for {entry['entities']} row(s):\n"
              f"  python src/smartskip.py pay --id {entry['bulkSkipId']} "
              f"--confirm-rows {entry['entities']}")
        return 0

    if args.cmd == "pay":
        if not args.bulk_id or args.confirm_rows is None:
            parser.error("--id and --confirm-rows required (payment is irreversible)")
        return 0 if client.pay(args.bulk_id, args.confirm_rows, args.pm or "") else 1

    if args.cmd == "status":
        for item in client.status(args.bulk_id or ""):
            print(json.dumps(item, indent=2))
        return 0

    if args.cmd == "download":
        if not (args.bulk_id and args.out):
            parser.error("--id and --out required")
        client.download(args.bulk_id, args.out, args.format)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

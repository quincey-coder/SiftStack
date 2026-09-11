"""Multi-provider skip-trace orchestrator - SmartSkip + DirectSkip + Trestle.

Takes ONE DataSift records export, runs both skip-trace vendors on every record,
Trestle-scores every number, then MERGES the two providers into one DataSift-ready
upload CSV with full provenance: every phone and every relative is labelled with
which vendor found it (SmartSkip / DirectSkip / both), and a discrepancies block
calls out what each vendor returned that the other didn't (SmartSkip gives
relationship-labelled relatives; DirectSkip gives name+phones only).

Numbers are prioritised by Trestle tier. When a record has more numbers than the
DataSift phone cap, the WORST are evicted first (invalid -> Drop -> Dial Fourth ->
unscored -> Dial Third), so Dial First / Dial Second always survive.

This is an operator CLI. It does NOT upload to DataSift (that stays manual /
Playwright - the API is read-only by policy). It only reads the export and writes
the upload CSV.

MONEY SAFETY:
  * `estimate` spends nothing - it prints the cost rundown only.
  * `run` is a HARD STOP before any spend: it prints the rundown, then BLOCKS until
    a person at an interactive terminal types the --max-cost amount back. If stdin
    or stdout is not a terminal (piped, scripted, or run by an agent's shell tool)
    it refuses with nothing spent. `run()` itself refuses without the
    SpendApproval that only `request_approval()` issues.
  * --max-cost is a hard ceiling across all three providers. SmartSkip's charge
    counts against it the moment payment is attempted, even if the order fails.
  * DirectSkip no-match is free; Trestle dedupes before billing.

CLI:
    python src/skip_orchestrator.py estimate --input export.csv
    python src/skip_orchestrator.py run --input export.csv --max-cost 50 --out upload.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import config
import directskip
import smartskip
from notice_parser import NoticeData, _detect_entity_type
from phone_validator import COST_PER_PHONE, DEFAULT_ADD_LITIGATOR, process_phones
from smartskip import _addr_key, _name_key, _split_owner_name

logger = logging.getLogger(__name__)

SS_COST = smartskip.COST_PER_HIT          # 0.15 / hit
DS_COST = directskip.COST_PER_HIT         # 0.10 / hit
TRESTLE_COST = COST_PER_PHONE             # 0.015 / number

# Estimate band for numbers-per-record (only used for the pre-run Trestle range).
EST_NUMS_LOW = 6
EST_NUMS_HIGH = 14

DEFAULT_PHONE_CAP = 30
DEFAULT_MAX_EMAILS = 6

# Tier keep-priority when a record is over the phone cap (lower = kept first).
# Mirrors clean_directskip.py so the two agree on eviction order.
_TIER_KEEP_RANK = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2,
                   "Dial Fourth": 4, "Drop": 5}
_UNSCORED_RANK = 3
_INVALID_RANK = 6

_PROVIDER_ORDER = ("SmartSkip", "DirectSkip")


class OrchestratorError(RuntimeError):
    """A fatal orchestration error - raised loudly, never swallowed to a zero result."""


# -- normalized merged model -------------------------------------------

@dataclass
class MergedPhone:
    digits: str
    ptype: str = "other"                      # mobile / landline / other
    providers: set = field(default_factory=set)   # {"SmartSkip","DirectSkip"}
    tier: str = ""                            # Trestle tier
    score: int | None = None
    valid: bool | None = None
    litigator: bool | None = None

    @property
    def mark(self) -> str:
        return {"mobile": "M", "landline": "L"}.get(self.ptype, "O")


@dataclass
class MergedPerson:
    role: str                                 # "owner" / "relative" / "unverified"
    name: str
    relationship: str = ""                    # from SmartSkip; "" for DirectSkip-only
    relationship_confirmed: bool = False
    age: str = ""
    deceased: bool = False
    providers: set = field(default_factory=set)
    phones: list = field(default_factory=list)   # list[MergedPhone]


@dataclass
class MergedRecord:
    notice: object                            # source NoticeData (identity/address)
    owner: MergedPerson
    others: list = field(default_factory=list)   # list[MergedPerson]
    emails: list = field(default_factory=list)
    providers: set = field(default_factory=set)  # any provider that returned anything
    deceased: bool = False

    @property
    def all_people(self):
        return [self.owner] + list(self.others)

    @property
    def has_results(self):
        return any(p.phones for p in self.all_people)


# -- phone-type + provider helpers -------------------------------------

_NAME_SUFFIXES = {"II", "III", "IV", "JR", "SR", "MD", "DDS"}


def _title(name: str) -> str:
    """Title-case a name without reordering parts (vendors return ALL-CAPS).

    ALL-CAPS names are a repo-wide red flag — every correct path title-cases.
    Mirrors clean_directskip.title_name; never rearranges (no NAMELF flip).
    """
    n = (name or "").strip()
    if not n:
        return ""
    out = []
    for w in re.split(r"\s+", n):
        u = w.upper().rstrip(".")
        if len(w) <= 1:
            out.append(w.upper())
        elif u in _NAME_SUFFIXES:
            out.append(w.upper())
        elif w.upper().startswith("MC") and len(w) > 2:
            out.append("Mc" + w[2].upper() + w[3:].lower())
        else:
            out.append(w[0].upper() + w[1:].lower())
    return " ".join(out)


def _norm_type(raw) -> str:
    t = (raw or "").strip().lower()
    if t.startswith("mobile"):
        return "mobile"
    if t.startswith("residential") or t.startswith("landline"):
        return "landline"
    return "other"


def _prov_label(providers) -> str:
    ordered = [p for p in _PROVIDER_ORDER if p in providers]
    return "+".join(ordered) if ordered else "?"


def _person_add_phone(person: MergedPerson, digits: str, ptype, provider: str) -> None:
    if not digits:
        return
    nt = _norm_type(ptype)
    for mp in person.phones:
        if mp.digits == digits:
            mp.providers.add(provider)
            if nt != "other" and mp.ptype == "other":
                mp.ptype = nt
            return
    person.phones.append(MergedPhone(digits=digits, ptype=nt, providers={provider}))


# -- 1. read the DataSift export into NoticeData -----------------------

# logical field -> candidate header names in a DataSift export (or a plain CSV).
_EXPORT_COLS = {
    "first": ("Owner First Name", "first_name", "First Name"),
    "last": ("Owner Last Name", "Last Name"),
    "owner_full": ("owner_name", "Owner Name", "full_name"),
    "business": ("FULL NAME/COMPANY/TRUST", "Business Name"),
    # Note: DataSift EXPORTS use Title-first-word headers ("Property address",
    # "Apn"); the UPLOAD format uses "Property Street Address"/"APN". Matching is
    # case/punctuation-insensitive (see _resolve), so both spellings resolve.
    "street": ("Property Street Address", "Property address", "address", "property_street"),
    "city": ("Property City", "Property city", "city"),
    "state": ("Property State", "Property state", "state"),
    # Prefer the 5-digit zip5 column over the ZIP+4 when both are present.
    "zip": ("Property zip5", "Property ZIP Code", "Property zip", "zip", "property_zip"),
    "mail_street": ("Mailing Street Address", "Mailing address", "mailing_address"),
    "mail_city": ("Mailing City", "Mailing city", "owner_city"),
    "mail_state": ("Mailing State", "Mailing state", "owner_state"),
    "mail_zip": ("Mailing zip5", "Mailing ZIP Code", "Mailing zip", "owner_zip"),
    "parcel": ("APN", "Apn", "Parcel ID", "Parcel id", "parcel_id", "parcel"),
    "county": ("Property County", "County", "county"),
    "tags": ("Tags", "tags"),
}


def _norm_hdr(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _resolve(header: list[str]) -> dict:
    """Map each logical field to the actual header present — case/punct-blind."""
    by_norm = {}
    for h in header:
        by_norm.setdefault(_norm_hdr(h), h)   # first occurrence wins
    out = {}
    for field_name, cands in _EXPORT_COLS.items():
        out[field_name] = next((by_norm[_norm_hdr(c)] for c in cands
                                if _norm_hdr(c) in by_norm), None)
    return out


def load_datasift_export(path: str | Path) -> list[NoticeData]:
    """Read a DataSift records export (or a compatible CSV) into NoticeData.

    Read-only: nothing is created or sent back to DataSift. Only the fields the
    skip-trace vendors need (owner name + property/mailing address) are pulled.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        col = _resolve(header)
        if not (col["street"] and (col["first"] or col["owner_full"] or col["business"])):
            raise OrchestratorError(
                f"{path.name}: doesn't look like a records export - need a property "
                f"address column and an owner name column. Found headers: {header[:8]}...")
        rows = list(reader)

    def g(row, key):
        c = col[key]
        return (row.get(c) or "").strip() if c else ""

    notices = []
    for row in rows:
        first, last = g(row, "first"), g(row, "last")
        owner = f"{first} {last}".strip() or g(row, "owner_full")
        business = g(row, "business")
        n = NoticeData(
            owner_name=owner,
            address=g(row, "street"), city=g(row, "city"),
            state=g(row, "state") or "TX", zip=g(row, "zip"),
            mailing_address=g(row, "mail_street"),
            owner_city=g(row, "mail_city"), owner_state=g(row, "mail_state"),
            owner_zip=g(row, "mail_zip"),
            parcel_id=g(row, "parcel"), county=g(row, "county"),
        )
        # Mark entities so both vendors skip them (they need a first+last person).
        etype = _detect_entity_type(owner) if owner else ""
        if business or etype:
            n.business_name = business or owner
            n.entity_type = etype or "other"
        # Carry a deceased hint from the record's own Tags (probate lists mark
        # "deceased"); the merge ORs this with whatever the vendors report.
        if re.search(r"\bdeceased\b", g(row, "tags"), re.I):
            n.owner_deceased = "yes"
        notices.append(n)
    logger.info("Read %d record(s) from %s", len(notices), path.name)
    return notices


def _is_eligible(n) -> bool:
    if getattr(n, "business_name", "") or getattr(n, "entity_type", ""):
        return False
    first, last = _split_owner_name(getattr(n, "owner_name", ""))
    return bool(first and last)


# -- 2. cost estimate (free) -------------------------------------------

def estimate(notices, run_smartskip=True, run_directskip=True) -> dict:
    eligible = [n for n in notices if _is_eligible(n)]
    n = len(eligible)
    ss = n * SS_COST if run_smartskip else 0.0
    ds = n * DS_COST if run_directskip else 0.0
    tr_low = n * EST_NUMS_LOW * TRESTLE_COST
    tr_high = n * EST_NUMS_HIGH * TRESTLE_COST
    est = {
        "records_in": len(notices), "eligible": n,
        "entities_skipped": len(notices) - n,
        "smartskip": round(ss, 2), "directskip_ceiling": round(ds, 2),
        "trestle_low": round(tr_low, 2), "trestle_high": round(tr_high, 2),
        "total_low": round(ss + ds + tr_low, 2),
        "total_high": round(ss + ds + tr_high, 2),
    }
    est["per_record_low"] = round(est["total_low"] / n, 3) if n else 0.0
    est["per_record_high"] = round(est["total_high"] / n, 3) if n else 0.0
    return est


def format_estimate(est: dict) -> str:
    L = [
        "=== SKIP-TRACE COST RUNDOWN ===",
        f"  Records in file        : {est['records_in']}",
        f"  Eligible (person)      : {est['eligible']}",
        f"  Entities skipped       : {est['entities_skipped']}",
        "",
        f"  SmartSkip  ({SS_COST:.2f}/rec)   : ${est['smartskip']:.2f}",
        f"  DirectSkip ({DS_COST:.2f}/rec)   : ${est['directskip_ceiling']:.2f}  (ceiling - misses are free)",
        f"  Trestle    ({TRESTLE_COST:.3f}/num) : ${est['trestle_low']:.2f} - ${est['trestle_high']:.2f}  "
        f"(est {EST_NUMS_LOW}-{EST_NUMS_HIGH} numbers/rec)",
        "  " + "-" * 40,
        f"  ESTIMATED TOTAL        : ${est['total_low']:.2f} - ${est['total_high']:.2f}",
        f"  Per record             : ${est['per_record_low']:.3f} - ${est['per_record_high']:.3f}",
        "",
        "  Litigator add-on billed by Trestle on top (rate unpublished).",
        "  Nothing has been spent.",
    ]
    return "\n".join(L)


# -- 2b. the approval gate (hard stop before any spend) ----------------

@dataclass(frozen=True)
class SpendApproval:
    """Proof that a person typed the spend approval at a terminal.

    Only request_approval() creates one; run() refuses to spend without it, so
    the approval can't be skipped by calling run() directly.
    """
    max_cost: float
    eligible: int
    smartskip: bool
    directskip: bool


def _check_ceiling(n_eligible, max_cost, run_smartskip, run_directskip) -> None:
    if max_cost <= 0:
        raise OrchestratorError(f"--max-cost must be positive (got {max_cost}).")
    ceiling = n_eligible * ((SS_COST if run_smartskip else 0) + (DS_COST if run_directskip else 0))
    if ceiling > max_cost:
        raise OrchestratorError(
            f"Estimated skip ceiling ${ceiling:.2f} exceeds --max-cost ${max_cost:.2f}. "
            f"Raise --max-cost or reduce the record count. Nothing was spent.")


def _is_interactive() -> bool:
    """True only when a person is at a real terminal (both ends are TTYs).

    Agent shell tools capture stdout and pipes/scripts redirect stdin, so either
    check failing means nobody can be there to approve.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str) -> str:
    return input(prompt)


# Chat approval ("reply yes in chat"):
#   1. `estimate` writes a PENDING approval (file + ceiling + batch). Pending is
#      not permission - anyone/anything can make one by running the free estimate.
#   2. The owner replies "yes" in chat. The project UserPromptSubmit hook
#      (.claude/hooks/skip_spend_approve.py) fires ONLY on the owner's own
#      messages - Claude cannot trigger it - and turns the pending into a
#      one-time APPROVAL TOKEN via approve_pending().
#   3. `run` spends only if it finds a fresh token for this exact file + ceiling
#      + batch, and burns it. No token -> refuses (fail-closed: if a hook breaks,
#      nothing spends). A PreToolUse guard (.claude/hooks/skip_spend_gate.py)
#      blocks shell/Write access to these files and the approval internals.
_STATE_DIR = Path(config.PROJECT_ROOT) / "data" / "skip_orchestrator"
PENDING_PATH = _STATE_DIR / "pending_approval.json"
APPROVAL_TOKEN_PATH = _STATE_DIR / "approval_token.json"
PENDING_TTL_S = 3600
APPROVAL_TOKEN_TTL_S = 900


def _norm_path(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def suggested_ceiling(est: dict) -> float:
    """Default --max-cost: the estimate's high end, rounded up to a whole dollar."""
    return float(math.ceil(est["total_high"]))


def write_pending_approval(input_path, est, max_cost, run_smartskip, run_directskip) -> None:
    """Called by `estimate`. Records what the owner would be approving."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps({
        "input": _norm_path(input_path), "file": Path(input_path).name,
        "max_cost": round(float(max_cost), 2), "eligible": int(est["eligible"]),
        "total_low": est["total_low"], "total_high": est["total_high"],
        "smartskip": bool(run_smartskip), "directskip": bool(run_directskip),
        "created": time.time(),
    }), encoding="utf-8")


def write_approval_token(input_path, max_cost, eligible, run_smartskip, run_directskip) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    APPROVAL_TOKEN_PATH.write_text(json.dumps({
        "input": _norm_path(input_path), "max_cost": round(float(max_cost), 2),
        "eligible": int(eligible), "smartskip": bool(run_smartskip),
        "directskip": bool(run_directskip), "created": time.time(),
    }), encoding="utf-8")


def approve_pending(ceiling_override: float | None = None) -> dict:
    """Pending -> approval token. Called ONLY by the owner's-message hook.

    Returns the approved pending (with the final max_cost). Raises
    OrchestratorError if there is nothing fresh to approve or the ceiling can't
    cover the skip cost.
    """
    try:
        pend = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OrchestratorError("there's no pending skip-trace estimate to approve") from None
    if time.time() - float(pend.get("created", 0)) > PENDING_TTL_S:
        PENDING_PATH.unlink(missing_ok=True)
        raise OrchestratorError("the pending estimate expired; re-run the estimate")
    max_cost = round(float(ceiling_override if ceiling_override is not None else pend["max_cost"]), 2)
    _check_ceiling(pend["eligible"], max_cost, pend["smartskip"], pend["directskip"])
    write_approval_token(pend["input"], max_cost, pend["eligible"],
                         pend["smartskip"], pend["directskip"])
    PENDING_PATH.unlink(missing_ok=True)
    return {**pend, "max_cost": max_cost}


def cancel_pending() -> bool:
    try:
        PENDING_PATH.unlink()
        return True
    except OSError:
        return False


def _consume_approval_token(input_path, max_cost, eligible, run_smartskip, run_directskip) -> None:
    """Accept + delete the hook's token if it matches this exact run; else raise."""
    try:
        tok = json.loads(APPROVAL_TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tok = None
    try:
        APPROVAL_TOKEN_PATH.unlink()          # single use, even when it doesn't match
    except OSError:
        pass
    problem = None
    if not tok:
        problem = "the owner hasn't approved this in chat"
    elif time.time() - float(tok.get("created", 0)) > APPROVAL_TOKEN_TTL_S:
        problem = "the approval expired"
    elif input_path is None or tok.get("input") != _norm_path(input_path):
        problem = "the approval was for a different file"
    elif tok.get("max_cost") != round(float(max_cost), 2):
        problem = f"the approval was for a ${tok.get('max_cost')} ceiling, not ${max_cost:.2f}"
    elif (tok.get("eligible") != eligible or tok.get("smartskip") != bool(run_smartskip)
          or tok.get("directskip") != bool(run_directskip)):
        problem = "the approval was for a different batch or vendor set"
    if problem:
        raise OrchestratorError(
            f"Not approved: {problem}. Run the estimate, then the owner replies 'yes' in "
            f"chat (or runs this in a terminal and types the ceiling). Nothing was spent.")


def request_approval(notices, max_cost: float, run_smartskip=True,
                     run_directskip=True, input_path=None) -> SpendApproval:
    """Show the cost rundown and STOP until a person approves the spend ceiling.

    This is the hard stop between the free estimate and any vendor spend. Two
    ways to approve, both from the person, never the agent:
      * interactive terminal: type the exact ceiling back;
      * Claude Code chat: reply "yes" after the estimate (the owner's-message
        hook turns the pending estimate into a one-time token).
    Anything else cancels with nothing spent.
    """
    est = estimate(notices, run_smartskip=run_smartskip, run_directskip=run_directskip)
    if not est["eligible"]:
        raise OrchestratorError("No eligible (person) records to skip trace. Nothing was spent.")
    _check_ceiling(est["eligible"], max_cost, run_smartskip, run_directskip)

    vendors = [v for v, on in (("SmartSkip", run_smartskip), ("DirectSkip", run_directskip)) if on]
    print(format_estimate(est))
    print("")
    print("=== APPROVAL REQUIRED BEFORE ANY SPEND ===")
    print(f"  Vendors                : {' + '.join(vendors + ['Trestle'])}")
    print(f"  Spend ceiling          : ${max_cost:.2f}  (hard stop across all vendors)")
    if run_smartskip:
        print("  SmartSkip charges your saved card.")
    if max_cost < est["total_high"]:
        print(f"  Note: ceiling is below the high estimate (${est['total_high']:.2f}); "
              "some numbers may stay unscored.")
    print("")

    if not _is_interactive():
        _consume_approval_token(input_path, max_cost, est["eligible"],
                                run_smartskip, run_directskip)
        logger.info("Spend approved by the owner in chat: ceiling $%.2f for %d record(s).",
                    max_cost, est["eligible"])
        return SpendApproval(max_cost=float(max_cost), eligible=est["eligible"],
                             smartskip=bool(run_smartskip), directskip=bool(run_directskip))

    try:
        answer = _ask(f"Type {max_cost:.2f} to approve spending up to ${max_cost:.2f} "
                      "(anything else cancels): ")
    except (EOFError, KeyboardInterrupt):
        raise OrchestratorError("Approval cancelled. Nothing was spent.") from None
    try:
        typed = float((answer or "").strip().lstrip("$").replace(",", ""))
    except ValueError:
        typed = None
    if typed is None or round(typed, 2) != round(max_cost, 2):
        raise OrchestratorError(f"Not approved (typed {answer!r}). Nothing was spent.")

    logger.info("Spend approved at the terminal: ceiling $%.2f for %d record(s).",
                max_cost, est["eligible"])
    return SpendApproval(max_cost=float(max_cost), eligible=est["eligible"],
                         smartskip=bool(run_smartskip), directskip=bool(run_directskip))


# -- 3. run the vendors ------------------------------------------------

def _run_directskip(eligible, budget) -> tuple[dict, float]:
    """Returns ({addr_key: DirectSkipRecord}, spent)."""
    client = directskip.DirectSkipClient()
    out, spent = {}, 0.0
    for n in eligible:
        if round(spent + DS_COST, 2) > budget:
            logger.warning("DirectSkip: budget $%.2f reached; stopping.", budget)
            break
        rec = client.search_record(n)
        if rec is None or rec.no_match:
            continue
        spent += DS_COST
        out[_addr_key(getattr(n, "address", ""))] = rec
    logger.info("DirectSkip: %d hit(s), $%.2f", len(out), spent)
    return out, round(spent, 2)


def _run_smartskip(eligible, tmp_csv, ledger: dict) -> dict:
    """Submit -> pay -> wait -> download -> parse. Returns {addr_key: rec}.

    Records the card charge in ledger["smartskip"] BEFORE paying: once pay() is
    called the card may be billed even if pay/wait/download then fails, and an
    uncounted charge would hand DirectSkip + Trestle the full ceiling again.
    """
    client = smartskip.SmartSkipClient()
    smartskip.build_trace_csv(eligible, tmp_csv)
    entry = client.submit(tmp_csv)                       # free: upload + calculate
    billable = int(entry.get("entities") or 0)
    if billable > len(eligible):
        raise OrchestratorError(
            f"SmartSkip calculated {billable} billable row(s) but only {len(eligible)} "
            f"were approved. Refusing to pay.")
    ledger["smartskip"] = round(billable * SS_COST, 2)
    if not client.pay(entry["bulkSkipId"], confirm_rows=billable):
        raise OrchestratorError("SmartSkip payment did not succeed - see log.")
    client.wait_for_completion(entry["bulkSkipId"])
    out_export = Path(tmp_csv).with_name("smartskip_export.csv")
    client.download(entry["bulkSkipId"], out_export)
    records = smartskip.parse_export(out_export)
    return {_addr_key(r.property_address): r for r in records if r.property_address}


# -- 4. merge ----------------------------------------------------------

def merge_record(notice, ss_rec, ds_rec) -> MergedRecord:
    owner = MergedPerson(role="owner",
                         name=(getattr(notice, "owner_name", "") or "").strip())
    # The export can carry its own deceased signal (probate lists); honour it.
    owner.deceased = bool(getattr(notice, "owner_deceased", ""))
    providers = set()

    # Owner subject phones - union of both vendors. DirectSkip address-only
    # matches (AB1/AB2) return a DIFFERENT person, so those never go on the owner.
    if ss_rec:
        providers.add("SmartSkip")
        if ss_rec.first or ss_rec.last:
            owner.name = f"{ss_rec.first} {ss_rec.last}".strip() or owner.name
        owner.deceased = owner.deceased or ss_rec.deceased_flag
        for ph in ss_rec.subject_phones:
            _person_add_phone(owner, ph["number"], ph.get("type"), "SmartSkip")
    ds_unverified = None
    if ds_rec:
        providers.add("DirectSkip")
        owner.deceased = owner.deceased or ds_rec.deceased_flag
        if ds_rec.address_only_match:
            # Keep, but as an unverified household contact - NOT the owner.
            ds_unverified = MergedPerson(
                role="unverified",
                name=_title(f"{ds_rec.first} {ds_rec.last}".strip()) or "(address match)",
                relationship="address-only match - verify identity",
                providers={"DirectSkip"})
            for ph in ds_rec.subject_phones:
                _person_add_phone(ds_unverified, ph["number"], ph.get("type"), "DirectSkip")
        else:
            if (ds_rec.first or ds_rec.last) and owner.name == (getattr(notice, "owner_name", "") or "").strip():
                owner.name = f"{ds_rec.first} {ds_rec.last}".strip() or owner.name
            for ph in ds_rec.subject_phones:
                _person_add_phone(owner, ph["number"], ph.get("type"), "DirectSkip")
    if owner.phones:
        owner.providers = set().union(*(p.providers for p in owner.phones))

    # Relatives - union, matched by name; SmartSkip's relationship wins.
    by_key = {}

    def add_relatives(people, provider, has_relationship):
        for p in people:
            key = tuple(_name_key(p.first, p.last) or [(p.name or "").upper(), ""])
            mp = by_key.get(key)
            if not mp:
                mp = MergedPerson(role="relative", name=_title(p.name),
                                  age=getattr(p, "age", "") or "",
                                  deceased=getattr(p, "deceased_flag", False))
                by_key[key] = mp
            mp.providers.add(provider)
            rel = getattr(p, "relationship", "")
            if has_relationship and rel and rel != "relative" and not mp.relationship:
                mp.relationship = rel
                mp.relationship_confirmed = True
            for ph in p.phones:
                _person_add_phone(mp, ph["number"], ph.get("type"), provider)

    if ss_rec:
        add_relatives(ss_rec.relatives, "SmartSkip", True)
    if ds_rec:
        add_relatives(ds_rec.relatives, "DirectSkip", False)

    others = list(by_key.values())
    if ds_unverified:
        others.insert(0, ds_unverified)

    owner.name = _title(owner.name)   # vendors return ALL-CAPS; normalize
    emails = list(dict.fromkeys(e.lower() for e in (ds_rec.subject_emails if ds_rec else []) if e))
    return MergedRecord(notice=notice, owner=owner, others=others, emails=emails,
                        providers=providers, deceased=owner.deceased)


# -- 5. Trestle scoring ------------------------------------------------

def _all_digits(merged) -> list[str]:
    seen, out = set(), []
    for rec in merged:
        for person in rec.all_people:
            for mp in person.phones:
                if mp.digits not in seen:
                    seen.add(mp.digits)
                    out.append(mp.digits)
    return out


def score_all(merged, budget, api_key=None, add_litigator=DEFAULT_ADD_LITIGATOR) -> dict:
    """Trestle-score every unique number (within budget). Returns {digits: info}."""
    api_key = api_key or getattr(config, "TRESTLE_API_KEY", "")
    numbers = _all_digits(merged)
    if not api_key:
        logger.warning("No TRESTLE_API_KEY - numbers will be uploaded UNSCORED.")
        return {}
    affordable = int(budget / TRESTLE_COST) if budget > 0 else 0
    if affordable < len(numbers):
        logger.warning("Trestle budget covers %d of %d numbers; the rest stay UNSCORED.",
                       max(affordable, 0), len(numbers))
        numbers = numbers[:max(affordable, 0)]
    if not numbers:
        return {}
    results, _errors = process_phones([(d, d) for d in numbers], api_key,
                                      add_litigator=add_litigator)
    score_map = {}
    for r in results:
        score_map[r["phone_number"]] = {
            "tier": r.get("assigned_tag") or "Unknown",
            "score": r.get("activity_score"),
            "valid": r.get("is_valid"),
            "litigator": r.get("is_litigator_risk"),
        }
    return score_map


def apply_scores(merged, score_map) -> None:
    for rec in merged:
        for person in rec.all_people:
            for mp in person.phones:
                info = score_map.get(mp.digits)
                if info:
                    mp.tier = info["tier"]
                    mp.score = info["score"]
                    mp.valid = info["valid"]
                    mp.litigator = info["litigator"]


# -- 6. tier-priority eviction to the phone cap ------------------------

def _keep_rank(mp: MergedPhone) -> int:
    if mp.valid is False:
        return _INVALID_RANK
    return _TIER_KEEP_RANK.get(mp.tier, _UNSCORED_RANK)


def _ordered_unique_phones(rec: MergedRecord) -> list:
    """Every unique number on the record, in dial-priority order (owner first)."""
    seen, ordered = set(), []
    for person in rec.all_people:
        for mp in person.phones:
            if mp.digits not in seen:
                seen.add(mp.digits)
                ordered.append(mp)
    return ordered


def select_survivors(phones: list, cap: int) -> tuple[list, list]:
    """Keep the best `cap` numbers; evict worst tier first. Preserves order."""
    if len(phones) <= cap:
        return list(phones), []
    ranked = sorted(range(len(phones)), key=lambda i: (_keep_rank(phones[i]), i))
    keep = set(ranked[:cap])
    kept = [p for i, p in enumerate(phones) if i in keep]
    cut = [p for i, p in enumerate(phones) if i not in keep]
    return kept, cut


# -- 7. Notes + tags + write -------------------------------------------

def _tier_label(mp: MergedPhone) -> str:
    if mp.valid is False:
        return " [invalid]"
    if not mp.tier or mp.tier == "Unknown":
        return ""
    return f" [{mp.tier} {mp.score}]" if mp.score is not None else f" [{mp.tier}]"


def _fmt_phone(d: str) -> str:
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else d


def _person_header(p: MergedPerson) -> str:
    if p.role == "owner":
        label = "OWNER (DECEASED)" if p.deceased else "OWNER"
    elif p.role == "unverified":
        label = "UNVERIFIED (DirectSkip address-only match - NOT the owner)"
    else:
        rel = p.relationship or "relationship unconfirmed"
        label = f"RELATIVE - {rel}"
        if p.deceased:
            label += " [DECEASED]"
    who = f"{label}: {p.name}" if p.name else label
    if p.age:
        who += f", age {p.age}"
    return who


def build_notes(rec: MergedRecord, slot_of: dict, cut: list, stamp: str) -> str:
    n = [f"=== SKIP TRACE (SmartSkip + DirectSkip) - {stamp} ===",
         f"Providers on this record: {_prov_label(rec.providers) or 'none'}"]
    if rec.deceased:
        n += ["", "** OWNER REPORTED DECEASED - the decision maker is a relative "
              "below, not the owner. **"]

    n += ["", "--- WHO EACH NUMBER BELONGS TO (dial priority) ---",
          "Look up the number you're calling. Tag = Trestle tier; source = which vendor."]
    for person in rec.all_people:
        if not person.phones:
            continue
        n += ["", _person_header(person)]
        for mp in person.phones:
            line = f"  {_fmt_phone(mp.digits)} ({mp.mark}){_tier_label(mp)} [{_prov_label(mp.providers)}]"
            if mp.litigator:
                # TCPA: litigator numbers are kept OFF the dial list, shown here only.
                line += "  <- (!) LITIGATOR - DO NOT CALL this number (not uploaded)"
            elif mp.digits not in slot_of:
                line += "  <- CUT at phone cap, not uploaded"
            n.append(line)

    # -- Provenance / discrepancies --
    only_ss = [mp for mp in _ordered_unique_phones(rec) if mp.providers == {"SmartSkip"}]
    only_ds = [mp for mp in _ordered_unique_phones(rec) if mp.providers == {"DirectSkip"}]
    both = [mp for mp in _ordered_unique_phones(rec) if {"SmartSkip", "DirectSkip"} <= mp.providers]
    ss_only_rel = [p for p in rec.others if p.role == "relative" and p.providers == {"SmartSkip"}]
    ds_only_rel = [p for p in rec.others if p.role == "relative" and p.providers == {"DirectSkip"}]
    disc = []
    if both:
        disc.append(f"  Confirmed by BOTH vendors: {len(both)} number(s)")
    if only_ss:
        disc.append("  Only SmartSkip found: " + ", ".join(_fmt_phone(m.digits) for m in only_ss))
    if only_ds:
        disc.append("  Only DirectSkip found: " + ", ".join(_fmt_phone(m.digits) for m in only_ds))
    if ss_only_rel:
        disc.append("  Relatives only SmartSkip has (with relationship): "
                    + "; ".join(f"{p.name} ({p.relationship or 'rel'})" for p in ss_only_rel))
    if ds_only_rel:
        disc.append("  Relatives only DirectSkip has (no relationship): "
                    + "; ".join(p.name for p in ds_only_rel))
    if disc:
        n += ["", "--- PROVENANCE / DISCREPANCIES ---"] + disc

    if cut:
        total = len(slot_of) + len(cut)
        n += ["", f"=== {len(cut)} NUMBER(S) CUT AT THE {len(slot_of)}-PHONE CAP ===",
              f"Found {total}; lowest Trestle tiers were dropped first "
              "(Dial First/Second always kept). Dial these manually if needed:"]
        for mp in cut:
            n.append(f"  {_fmt_phone(mp.digits)} ({mp.mark}){_tier_label(mp)} [{_prov_label(mp.providers)}]")

    litigators = [mp for mp in _ordered_unique_phones(rec) if mp.litigator]
    if litigators:
        n += ["", f"=== {len(litigators)} LITIGATOR NUMBER(S) WITHHELD FROM THE DIAL LIST ===",
              "TCPA risk - these are listed above but were NOT uploaded to a phone slot. "
              "Reach this person on the other numbers instead:"]
        for mp in litigators:
            n.append(f"  {_fmt_phone(mp.digits)} ({mp.mark}){_tier_label(mp)} [{_prov_label(mp.providers)}]")

    n += ["", f"Numbers uploaded: {len(slot_of)} of "
          f"{len(_ordered_unique_phones(rec))} found "
          f"({len(litigators)} litigator withheld).  M=mobile L=landline O=other"]
    return "\n".join(n)


def _build_tags(rec: MergedRecord, stamp: str, over_cap: bool, no_phone: bool) -> str:
    tags = [f"skip_traced_{stamp}", "skip2"]
    for p in _PROVIDER_ORDER:
        if p in rec.providers:
            tags.append(p)
    tags.append("deceased" if rec.deceased else "living")
    if over_cap:
        tags.append("phone overflow")
    if no_phone:
        tags.append("no phone")
    return ",".join(tags)


def _output_columns(phone_cap: int, max_emails: int) -> list[str]:
    return (["Property Street Address", "Property City", "Property State",
             "Property ZIP Code", "Owner First Name", "Owner Last Name",
             "Mailing Street Address", "Mailing City", "Mailing State",
             "Mailing ZIP Code"]
            + [f"Phone {i}" for i in range(1, phone_cap + 1)]
            + [f"Email {i}" for i in range(1, max_emails + 1)]
            + ["Tags", "Notes", "Owner Deceased"])


def write_upload_csv(merged, out_path: str | Path, phone_cap: int = DEFAULT_PHONE_CAP,
                     max_emails: int = DEFAULT_MAX_EMAILS, stamp: str | None = None) -> dict:
    stamp = stamp or date.today().strftime("%m/%Y")
    cols = _output_columns(phone_cap, max_emails)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stats = {"records": 0, "phones": 0, "emails": 0, "over_cap": 0, "no_phone": 0,
             "cut_numbers": 0, "litigator_withheld": 0}

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for rec in merged:
            r = {c: "" for c in cols}
            n = rec.notice
            r["Property Street Address"] = getattr(n, "address", "")
            r["Property City"] = getattr(n, "city", "")
            r["Property State"] = getattr(n, "state", "") or "TX"
            r["Property ZIP Code"] = getattr(n, "zip", "")
            r["Mailing Street Address"] = getattr(n, "mailing_address", "") or getattr(n, "address", "")
            r["Mailing City"] = getattr(n, "owner_city", "") or getattr(n, "city", "")
            r["Mailing State"] = getattr(n, "owner_state", "") or getattr(n, "state", "") or "TX"
            r["Mailing ZIP Code"] = getattr(n, "owner_zip", "") or getattr(n, "zip", "")
            first, last = _split_owner_name(rec.owner.name)
            r["Owner First Name"], r["Owner Last Name"] = first, last
            r["Owner Deceased"] = "yes" if rec.deceased else ""

            ordered = _ordered_unique_phones(rec)
            # TCPA: litigator numbers never get a dial slot — they stay in Notes only.
            litigators = [mp for mp in ordered if mp.litigator]
            dialable = [mp for mp in ordered if not mp.litigator]
            kept, cut = select_survivors(dialable, phone_cap)
            slot_of = {}
            for i, mp in enumerate(kept, 1):
                r[f"Phone {i}"] = mp.digits
                slot_of[mp.digits] = i
            stats["phones"] += len(kept)
            stats["litigator_withheld"] += len(litigators)
            if not kept:
                stats["no_phone"] += 1
            if cut:
                stats["over_cap"] += 1
                stats["cut_numbers"] += len(cut)

            for i, email in enumerate(dict.fromkeys(rec.emails), 1):
                if i > max_emails:
                    break
                r[f"Email {i}"] = email
                stats["emails"] += 1

            r["Notes"] = build_notes(rec, slot_of, cut, stamp)
            r["Tags"] = _build_tags(rec, stamp, bool(cut), not kept)
            w.writerow(r)
            stats["records"] += 1

    logger.info("Wrote %d record(s) to %s - %d phones, %d emails, %d over cap (%d cut), "
                "%d litigator withheld",
                stats["records"], out_path, stats["phones"], stats["emails"],
                stats["over_cap"], stats["cut_numbers"], stats["litigator_withheld"])
    return stats


# -- orchestration -----------------------------------------------------

def run(notices, approval: SpendApproval,
        phone_cap: int = DEFAULT_PHONE_CAP, out_path: str | Path = "skip_upload.csv",
        stamp: str | None = None, tmp_dir: str | Path | None = None) -> dict:
    """Full pipeline under the approved hard cost ceiling. Returns a stats/cost dict.

    Spends nothing unless `approval` came from request_approval() for this batch.
    """
    if not isinstance(approval, SpendApproval):
        raise OrchestratorError(
            "run() needs a SpendApproval from request_approval() - a person must "
            "approve the spend at a terminal. Nothing was spent.")
    eligible = [n for n in notices if _is_eligible(n)]
    if not eligible:
        raise OrchestratorError("No eligible (person) records to skip trace.")
    if len(eligible) != approval.eligible:
        raise OrchestratorError(
            f"Approval covered {approval.eligible} record(s) but this batch has "
            f"{len(eligible)}. Nothing was spent.")
    max_cost = approval.max_cost
    run_smartskip, run_directskip = approval.smartskip, approval.directskip
    _check_ceiling(len(eligible), max_cost, run_smartskip, run_directskip)

    tmp_dir = Path(tmp_dir) if tmp_dir else (Path(config.PROJECT_ROOT) / "data" / "skip_orchestrator")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    spent = 0.0
    ds_records, ss_records = {}, {}
    # Provider order (owner preference, 2026-08-20): SmartSkip runs FIRST — it
    # is the primary vendor (grounded relatives WITH relationships), DirectSkip
    # second as the per-record cross-check. Was DirectSkip-first. If SmartSkip
    # fails, the run still degrades to DirectSkip-only exactly as before;
    # DirectSkip's budget is what remains under the ceiling after SmartSkip.
    if run_smartskip:
        ledger = {"smartskip": 0.0}
        try:
            ss_records = _run_smartskip(eligible, tmp_dir / "smartskip_input.csv", ledger)
        except (smartskip.SmartSkipError, OrchestratorError) as e:
            logger.error("SmartSkip failed (%s) - continuing DirectSkip-only.", e)
        spent += ledger["smartskip"]   # counted even when the order failed after paying
    if run_directskip:
        ds_records, ds_spent = _run_directskip(eligible, budget=max(max_cost - spent, 0.0))
        spent += ds_spent

    merged = [merge_record(n, ss_records.get(_addr_key(getattr(n, "address", ""))),
                           ds_records.get(_addr_key(getattr(n, "address", ""))))
              for n in eligible]

    trestle_budget = max(max_cost - spent, 0.0)
    score_map = score_all(merged, trestle_budget)
    apply_scores(merged, score_map)
    spent += len(score_map) * TRESTLE_COST

    stats = write_upload_csv(merged, out_path, phone_cap=phone_cap, stamp=stamp)
    stats["cost"] = round(spent, 2)
    stats["directskip_hits"] = len(ds_records)
    stats["smartskip_hits"] = len(ss_records)
    stats["numbers_scored"] = len(score_map)
    logger.info("DONE - $%.2f spent (DS %d, SS %d, Trestle %d).",
                stats["cost"], len(ds_records), len(ss_records), len(score_map))
    return stats


# -- CLI ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["estimate", "run"])
    ap.add_argument("--input", required=True, help="DataSift records export CSV")
    ap.add_argument("--out", default="skip_upload.csv", help="output upload CSV (run)")
    ap.add_argument("--max-cost", type=float,
                    help="hard USD ceiling across all providers (required for run; "
                         "estimate defaults it to the high estimate, rounded up)")
    ap.add_argument("--phone-cap", type=int, default=DEFAULT_PHONE_CAP)
    ap.add_argument("--no-smartskip", action="store_true")
    ap.add_argument("--no-directskip", action="store_true")
    ap.add_argument("--stamp", help="MM/YYYY tag stamp (default: this month)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    notices = load_datasift_export(args.input)
    rs, rd = not args.no_smartskip, not args.no_directskip

    if args.cmd == "estimate":
        est = estimate(notices, run_smartskip=rs, run_directskip=rd)
        print(format_estimate(est))
        if not est["eligible"]:
            return 0
        ceiling = args.max_cost if args.max_cost is not None else suggested_ceiling(est)
        try:
            _check_ceiling(est["eligible"], ceiling, rs, rd)
        except OrchestratorError as e:
            print(f"\nSTOPPED: {e}", file=sys.stderr)
            return 2
        write_pending_approval(args.input, est, ceiling, rs, rd)
        print("")
        print("=== AWAITING OWNER APPROVAL ===")
        print(f"  Spend ceiling          : ${ceiling:.2f}  (hard stop across all vendors)")
        print("  Owner: reply 'yes' in chat to approve (or 'yes, cap $X' for a different")
        print(f"  ceiling). Pending expires in {PENDING_TTL_S // 60} min. Then:")
        print(f"    run --input \"{args.input}\" --max-cost {ceiling:g}")
        return 0

    if args.max_cost is None:
        ap.error("run requires --max-cost (a hard spend ceiling)")
    try:
        approval = request_approval(notices, args.max_cost, run_smartskip=rs,
                                    run_directskip=rd, input_path=args.input)
        stats = run(notices, approval, phone_cap=args.phone_cap,
                    out_path=args.out, stamp=args.stamp)
    except OrchestratorError as e:
        print(f"\nSTOPPED: {e}", file=sys.stderr)
        return 2
    print(f"\nWrote {stats['records']} record(s) to {args.out}")
    print(f"  DirectSkip hits {stats['directskip_hits']} | SmartSkip hits {stats['smartskip_hits']} "
          f"| numbers scored {stats['numbers_scored']}")
    print(f"  Phones uploaded {stats['phones']} | over cap {stats['over_cap']} "
          f"({stats['cut_numbers']} cut) | litigator withheld {stats['litigator_withheld']} "
          f"| no-phone {stats['no_phone']}")
    print(f"  TOTAL SPENT: ${stats['cost']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

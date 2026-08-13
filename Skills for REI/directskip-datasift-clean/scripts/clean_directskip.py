#!/usr/bin/env python3
"""Clean a DirectSkip / "contactinfo" skip-trace return into a DataSift upload CSV.

Standalone: reads ONLY the vendor file. No merge, no second source.

Every number the vendor found becomes a callable phone slot; the Notes field
records which person each number belongs to, keyed by the NUMBER (never by slot
position -- DataSift appends behind existing phones, so slot labels would point
at the wrong row).

When Trestle IQ scores are available (--trestle-results), records over the
30-phone cap drop their WORST-tier numbers first instead of whatever happened
to be collected last: invalid -> Drop -> Dial Fourth -> unscored -> Dial Third
-> Dial Second -> Dial First. Dial First / Dial Second numbers are the primary
dial targets and are always the last to be cut.

Usage:
    python clean_directskip.py --input vendor.csv --output cleaned.csv \
        [--stamp MM/YYYY] [--max-phones 30] [--max-emails 6] [--verify] \
        [--trestle-results validation_results.csv]
"""

import argparse
import csv
import re
import sys
from datetime import date

# DataSift's ceiling -- its own export format carries Phone 1-30.
DEFAULT_MAX_PHONES = 30
DEFAULT_MAX_EMAILS = 6

SUFFIXES = {"II", "III", "IV", "JR", "SR", "LLC", "INC", "MD", "DDS"}

# ── Trestle IQ tier ranking ───────────────────────────────────────────────
#
# Keep-priority when a record is over the phone cap: lower rank = kept first.
# Unscored numbers sit between Dial Third and Dial Fourth: a known-low number
# (21-40) is worse than an unknown, but an unknown must never outrank a
# scored-active one. Invalid (Trestle is_valid=false) is cut before anything.
TIER_KEEP_RANK = {
    "Dial First": 0,
    "Dial Second": 1,
    "Dial Third": 2,
    "Dial Fourth": 4,
    "Drop": 5,
}
UNSCORED_RANK = 3
INVALID_RANK = 6


def load_trestle_results(paths):
    """Load Trestle results into {10-digit phone: {tier, score, valid}}.

    Accepts either phone_validator output layout:
      - validation_results.csv  (phone_number, activity_score, assigned_tag,
        is_valid, ...) -- preferred, carries score + validity
      - phone_tags_for_datasift.csv  (Phone Number, Phone Tags)
    """
    trestle = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rdr = csv.DictReader(fh)
            flds = rdr.fieldnames or []
            if "phone_number" in flds:
                for row in rdr:
                    d = digits(row.get("phone_number", ""))
                    if not d:
                        continue
                    raw_score = (row.get("activity_score") or "").strip()
                    try:
                        score = int(float(raw_score))
                    except ValueError:
                        score = None
                    valid = {"true": True, "false": False}.get(
                        (row.get("is_valid") or "").strip().lower())
                    trestle[d] = {"tier": (row.get("assigned_tag") or "").strip(),
                                  "score": score, "valid": valid}
            elif "Phone Number" in flds and "Phone Tags" in flds:
                for row in rdr:
                    d = digits(row.get("Phone Number", ""))
                    if not d:
                        continue
                    trestle[d] = {"tier": (row.get("Phone Tags") or "").strip(),
                                  "score": None, "valid": None}
            else:
                sys.exit(f"ERROR: unrecognized Trestle results layout in {path}. "
                         "Expected validation_results.csv (phone_number/assigned_tag) "
                         "or phone_tags_for_datasift.csv (Phone Number/Phone Tags).")
    return trestle


def trestle_rank(d, trestle):
    """Keep-priority rank for one number. Lower = more desirable to keep."""
    info = (trestle or {}).get(d)
    if not info:
        return UNSCORED_RANK
    if info.get("valid") is False:
        return INVALID_RANK
    return TIER_KEEP_RANK.get(info.get("tier"), UNSCORED_RANK)


def tier_label(d, trestle):
    """Notes annotation like ' [Dial First 92]', or '' when unscored."""
    info = (trestle or {}).get(d)
    if not info:
        return ""
    if info.get("valid") is False:
        return " [invalid number]"
    t = info.get("tier") or ""
    if not t or t not in TIER_KEEP_RANK:
        return ""
    s = info.get("score")
    return f" [{t} {s}]" if s is not None else f" [{t}]"


def tier_stat_name(d, trestle):
    info = (trestle or {}).get(d)
    if not info:
        return "unscored"
    if info.get("valid") is False:
        return "invalid"
    t = info.get("tier")
    return t if t in TIER_KEEP_RANK else "unscored"


def select_survivors(ordered, max_phones, trestle=None):
    """Pick which unique numbers keep phone slots when a record is over the cap.

    Without Trestle data: first-come-first-served in dial-priority order
    (legacy behavior). With Trestle data: cut the worst tier first --
    invalid -> Drop -> Dial Fourth -> unscored -> Dial Third -> Dial Second
    -> Dial First -- ties broken by dial priority, so the owner's numbers
    outlast a Person-3 relative's within the same tier.

    Returns (kept, cut), both preserving the original dial-priority order.
    """
    if len(ordered) <= max_phones:
        return list(ordered), []
    if not trestle:
        return list(ordered[:max_phones]), list(ordered[max_phones:])
    ranked = sorted(range(len(ordered)),
                    key=lambda i: (trestle_rank(ordered[i], trestle), i))
    keep = set(ranked[:max_phones])
    kept = [d for i, d in enumerate(ordered) if i in keep]
    cut = [d for i, d in enumerate(ordered) if i not in keep]
    return kept, cut


# ── normalizers ───────────────────────────────────────────────────────────

def digits(phone):
    """Bare 10-digit form, or '' if it isn't a usable US number."""
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def fmt_phone(d):
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else d


def zip5(z):
    m = re.match(r"^\s*(\d{5})", z or "")
    return m.group(1) if m else ""


def title_name(name):
    """Title-case without ever reordering the parts.

    Vendor names arrive ALL-CAPS and ALL-CAPS output is a corruption red flag.
    Names arrive already split into first/last, so there is no NAMELF flip risk
    -- this must never rearrange anything.
    """
    n = (name or "").strip()
    if not n:
        return ""
    out = []
    for w in re.split(r"\s+", n):
        if len(w) <= 1:
            out.append(w.upper())
        elif w.upper().rstrip(".") in SUFFIXES:
            out.append(w.upper())
        elif w.upper().startswith("MC") and len(w) > 2:
            out.append("Mc" + w[2].upper() + w[3:].lower())
        else:
            out.append(w[0].upper() + w[1:].lower())
    return " ".join(out)


def fmt_addr(street, city, state, zipc):
    parts = []
    if (street or "").strip():
        parts.append(title_name(street))
    tail = []
    if (city or "").strip():
        tail.append(title_name(city))
    if (state or "").strip():
        tail.append(state.upper())
    z = zip5(zipc)
    if z:
        tail.append(z)
    if tail:
        parts.append(" ".join(tail))
    return ", ".join(parts)


def type_mark(vendor_type):
    t = (vendor_type or "").strip().lower()
    if t.startswith("mobile"):
        return "M"
    if t.startswith("residential") or t.startswith("landline"):
        return "L"
    return "O"


def type_rank(vendor_type):
    """Mobile first -- the best contact should always be Phone 1."""
    t = (vendor_type or "").strip().lower()
    if t.startswith("mobile"):
        return 0
    if t.startswith("residential") or t.startswith("landline"):
        return 1
    return 2


# ── row processing ────────────────────────────────────────────────────────

def person_phones(row, prefix, count=7, trestle=None):
    out = []
    for i in range(1, count + 1):
        d = digits(row.get(f"{prefix}Phone{i}", ""))
        if not d:
            continue
        vt = row.get(f"{prefix}Phone{i} Type", "")
        out.append({"digits": d, "rank": type_rank(vt), "mark": type_mark(vt)})
    # Best Trestle tier first within the person, phone type as tiebreak.
    # With no Trestle data every rank ties and this is the legacy type sort.
    return sorted(out, key=lambda p: (trestle_rank(p["digits"], trestle), p["rank"]))


def relative_phones(row, prefix, j, trestle=None):
    out = []
    for k in range(1, 6):
        d = digits(row.get(f"{prefix}Relative{j} Phone{k}", ""))
        if not d:
            continue
        vt = row.get(f"{prefix}Relative{j} Phone{k} Type", "")
        out.append({"digits": d, "rank": type_rank(vt), "mark": type_mark(vt)})
    return sorted(out, key=lambda p: (trestle_rank(p["digits"], trestle), p["rank"]))


def collect_people(row, deceased, trestle=None):
    """Every person on the record, in dial-priority order."""
    people = []

    mf = title_name(row.get("Matched First Name", ""))
    ml = title_name(row.get("Matched Last Name", ""))
    if not mf and not ml:
        mf = title_name(row.get("Input First Name", ""))
        ml = title_name(row.get("Input Last Name", ""))

    people.append({
        "label": "OWNER (DECEASED)" if deceased else "OWNER",
        "name": f"{mf} {ml}".strip(),
        "age": (row.get("Age") or "").strip(),
        "phones": person_phones(row, "", trestle=trestle),
    })

    for pi in (2, 3):
        pre = f"Person{pi} "
        pn = f'{title_name(row.get(pre + "First Name", ""))} ' \
             f'{title_name(row.get(pre + "Last Name", ""))}'.strip()
        if not pn:
            continue
        label = f"PERSON {pi} (additional owner/resident)"
        if (row.get(pre + "Deceased") or "").strip().upper() == "Y":
            label += " [DECEASED]"
        people.append({
            "label": label, "name": pn,
            "age": (row.get(pre + "Age") or "").strip(),
            "phones": person_phones(row, pre, trestle=trestle),
        })

    for pi in (0, 2, 3):
        pre = "" if pi == 0 else f"Person{pi} "
        for j in range(1, 6):
            rn = (row.get(f"{pre}Relative{j} Name") or "").strip()
            if not rn:
                continue  # a nameless number can't be attributed; don't dial it
            if pi == 0:
                label = (f"HEIR CANDIDATE {j} (relative of deceased owner)"
                         if deceased else f"RELATIVE {j} (of owner)")
            else:
                label = f"RELATIVE {j} (of Person {pi})"
            people.append({
                "label": label, "name": title_name(rn),
                "age": (row.get(f"{pre}Relative{j} Age") or "").strip(),
                "phones": relative_phones(row, pre, j, trestle=trestle),
            })

    return people


def build_notes(row, people, slot_of, overflow, stamp, deceased, suspect, rc,
                conf_addr, conf_differs, on_file_addr, trestle=None):
    n = [f"=== DIRECTSKIP SKIP TRACE - {stamp} ==="]

    owner = people[0]
    hdr = []
    if owner["name"]:
        hdr.append(f"Matched: {owner['name']}")
    if owner["age"]:
        hdr.append(f"age {owner['age']}")
    if rc:
        hdr.append(f"result {rc}")
    if hdr:
        n.append(" | ".join(hdr))

    if deceased:
        n += ["", "** OWNER REPORTED DECEASED - the decision maker is an heir "
                  "below, not the owner. **"]

    if suspect:
        n.append("")
        if not (row.get("Matched First Name") or "").strip() and \
           not (row.get("Matched Last Name") or "").strip():
            sent = f'{title_name(row.get("Input First Name",""))} ' \
                   f'{title_name(row.get("Input Last Name",""))}'.strip()
            n += ["** NO MATCH RETURNED - the vendor found no person for this record.",
                  f"   Input name: {sent}",
                  "   Nothing was added. Re-skip with a corrected owner name. **"]
        else:
            sent = f'{title_name(row.get("Input First Name",""))} ' \
                   f'{title_name(row.get("Input Last Name",""))}'.strip()
            n += [f"** LOW-CONFIDENCE MATCH ({rc}) - vendor matched on address, not name.",
                  f"   Input name: {sent} / Returned: {owner['name']}",
                  "   Verify identity before dialing. **"]

    if conf_differs:
        n += ["",
              "CONFIRMED MAILING ADDRESS (skip trace - NOT applied to record):",
              f"  {conf_addr}",
              f"  on file: {on_file_addr}"]

    # Keyed by the NUMBER, never by slot: the import appends behind existing
    # phones, so a slot label would be offset and point at the wrong row.
    n += ["", "--- WHO EACH NUMBER BELONGS TO (dial reference) ---",
          "Look up the number you are calling. Order below = dial priority."]

    overflow_by_person = []
    for p in people:
        if not p["phones"]:
            continue
        who = f"{p['label']}: {p['name']}"
        if p["age"]:
            who += f", age {p['age']}"
        n += ["", who]
        for ph in p["phones"]:
            f = fmt_phone(ph["digits"])
            tl = tier_label(ph["digits"], trestle)
            if ph["digits"] in slot_of:
                n.append(f"  {f} ({ph['mark']}){tl}")
            else:
                cut_msg = ("CUT at phone cap (lowest Trestle tier), not uploaded"
                           if trestle else "OVERFLOW, not uploaded, dial manually")
                n.append(f"  {f} ({ph['mark']}){tl}  <- {cut_msg}")
                overflow_by_person.append(
                    f"  {f} ({ph['mark']}){tl}  -  {p['label']}: {p['name']}")

    if overflow:
        total = len(slot_of) + len(overflow)
        if trestle:
            n += ["", f"=== CUT AT THE PHONE CAP - {len(overflow)} NOT UPLOADED ===",
                  f"This record found {total} numbers but DataSift only holds "
                  f"{len(slot_of)}.",
                  "Lowest Trestle tiers were cut first (invalid -> Drop ->",
                  "Dial Fourth -> unscored -> Dial Third); Dial First / Dial Second",
                  "are the primary targets and are always kept first.",
                  "The numbers below have NO phone slot - dial manually only if",
                  "the uploaded numbers dead-end.", ""]
        else:
            n += ["", f"=== OVERFLOW NUMBERS - {len(overflow)} NOT UPLOADED ===",
                  f"This record found {total} numbers but DataSift only holds "
                  f"{len(slot_of)}.",
                  "The numbers below have NO phone slot. They are recorded here only -",
                  "dial them manually from this list.", ""]
        seen = set()
        for line in overflow_by_person:
            if line not in seen:
                seen.add(line)
                n.append(line)

    n += ["", f"Numbers uploaded: {len(slot_of)} of "
              f"{len(slot_of) + len(overflow)} found.  "
              "M=mobile L=landline O=other"]
    return "\n".join(n)


def process(rows, stamp, max_phones, max_emails, trestle=None):
    cols = (["Property Street Address", "Property City", "Property State",
             "Property ZIP Code", "Owner First Name", "Owner Last Name",
             "Mailing Street Address", "Mailing City", "Mailing State",
             "Mailing ZIP Code"]
            + [f"Phone {i}" for i in range(1, max_phones + 1)]
            + [f"Email {i}" for i in range(1, max_emails + 1)]
            + ["Tags", "Notes", "Owner Deceased"])

    out, stats = [], {
        "phones": 0, "emails": 0, "overflow_recs": 0, "overflow_nums": 0,
        "deceased": 0, "suspect": 0, "addr_diff": 0, "no_phone": 0,
        "trimmed_recs": 0, "tier_violations": 0, "dropped_by_tier": {},
    }

    for row in rows:
        r = {c: "" for c in cols}

        r["Property Street Address"] = title_name(row.get("Input Property Address", ""))
        r["Property City"] = title_name(row.get("Input Property City", ""))
        r["Property State"] = (row.get("Input Property State") or "").upper()
        r["Property ZIP Code"] = zip5(row.get("Input Property Zip", ""))

        r["Mailing Street Address"] = title_name(row.get("Input Mailing Address", ""))
        r["Mailing City"] = title_name(row.get("Input Mailing City", ""))
        r["Mailing State"] = (row.get("Input Mailing State") or "").upper()
        r["Mailing ZIP Code"] = zip5(row.get("Input Mailing Zip", ""))

        deceased = (row.get("Deceased") or "").strip().upper() == "Y"
        rc = (row.get("ResultCode") or "").strip().upper()
        suspect = rc != "CI"
        if deceased:
            r["Owner Deceased"] = "yes"
            stats["deceased"] += 1
        if suspect:
            stats["suspect"] += 1

        people = collect_people(row, deceased, trestle=trestle)
        r["Owner First Name"], r["Owner Last Name"] = "", ""
        owner_parts = people[0]["name"].split(" ", 1)
        if owner_parts and owner_parts[0]:
            r["Owner First Name"] = owner_parts[0]
            if len(owner_parts) > 1:
                r["Owner Last Name"] = owner_parts[1]

        # slots: one per unique number, reported under every person who has it.
        # First gather every unique number in dial-priority order, then let
        # select_survivors decide who keeps a slot when over the cap (Trestle
        # tier eviction when scores are loaded, FCFS otherwise).
        ordered, seen_d = [], set()
        for p in people:
            for ph in p["phones"]:
                d = ph["digits"]
                if d not in seen_d:
                    seen_d.add(d)
                    ordered.append(d)

        kept, cut = select_survivors(ordered, max_phones, trestle)

        slot_of, overflow, slot = {}, list(cut), 0
        for d in kept:
            slot += 1
            slot_of[d] = slot
            r[f"Phone {slot}"] = d
            stats["phones"] += 1
        if slot == 0:
            stats["no_phone"] += 1
        if overflow:
            stats["overflow_recs"] += 1
            stats["overflow_nums"] += len(overflow)
            if trestle:
                stats["trimmed_recs"] += 1
                for d in overflow:
                    name = tier_stat_name(d, trestle)
                    stats["dropped_by_tier"][name] = \
                        stats["dropped_by_tier"].get(name, 0) + 1
                # Regression tripwire: every kept number must rank at least
                # as well as every cut number. Holds by construction today.
                if max(trestle_rank(d, trestle) for d in kept) > \
                   min(trestle_rank(d, trestle) for d in overflow):
                    stats["tier_violations"] += 1

        seen_email, eslot = set(), 0
        for pre in ("", "Person2 ", "Person3 "):
            for i in (1, 2):
                v = (row.get(f"{pre}Email{i}") or "").strip().lower()
                if not v or v in seen_email or eslot >= max_emails:
                    continue
                seen_email.add(v)
                eslot += 1
                r[f"Email {eslot}"] = v
                stats["emails"] += 1

        conf_addr = fmt_addr(row.get("Confirmed Mailing Address"),
                             row.get("Confirmed Mailing City"),
                             row.get("Confirmed Mailing State"),
                             row.get("Confirmed Mailing Zip"))
        on_file = fmt_addr(row.get("Input Mailing Address"),
                           row.get("Input Mailing City"),
                           row.get("Input Mailing State"),
                           row.get("Input Mailing Zip"))
        norm = lambda s: re.sub(r"[^A-Za-z0-9]", "", s or "").upper()
        conf_differs = bool(conf_addr) and norm(
            row.get("Confirmed Mailing Address")) != norm(
            row.get("Input Mailing Address"))
        if conf_differs:
            stats["addr_diff"] += 1

        r["Notes"] = build_notes(row, people, slot_of, overflow, stamp,
                                 deceased, suspect, rc, conf_addr,
                                 conf_differs, on_file, trestle=trestle)

        tags = ["skip2", "DirectSkip", f"Second Skip {stamp}"]
        tags.append("skip2 deceased" if deceased else "living")
        if suspect:
            tags.append("skip2 low confidence match")
        if conf_differs:
            tags.append("skip2 confirmed addr differs")
        if slot == 0:
            tags.append("skip2 no phone")
        if overflow:
            tags.append("skip2 phone overflow")
            if trestle:
                tags.append("skip2 trestle trimmed")
        r["Tags"] = ",".join(tags)

        out.append(r)

    return cols, out, stats


# ── verification ──────────────────────────────────────────────────────────

def verify(cols, rows, src_count, max_phones, max_emails, stats):
    """Every check here caught a real defect at least once."""
    checks, phone_re = [], re.compile(r"(\d{3})-(\d{3})-(\d{4})")

    gaps = dups = bad = edups = noprop = caps = badzip = missing = stale = 0
    overflow_labeled = set()
    overflow_expected = 0

    for r in rows:
        empty = False
        seen = set()
        for i in range(1, max_phones + 1):
            v = (r[f"Phone {i}"] or "").strip()
            if not v:
                empty = True
                continue
            if empty:
                gaps += 1
            if not re.fullmatch(r"\d{10}", v):
                bad += 1
            if v in seen:
                dups += 1
            seen.add(v)

        eseen = set()
        for i in range(1, max_emails + 1):
            v = (r[f"Email {i}"] or "").strip().lower()
            if v:
                if v in eseen:
                    edups += 1
                eseen.add(v)

        if not r["Property Street Address"].strip() or not r["Property ZIP Code"].strip():
            noprop += 1
        for f in ("Owner First Name", "Owner Last Name"):
            v = r[f]
            if len(v) > 1 and v.isupper() and v.isalpha():
                caps += 1
        for f in ("Property ZIP Code", "Mailing ZIP Code"):
            v = r[f].strip()
            if v and not re.fullmatch(r"\d{5}", v):
                badzip += 1

        notes = r["Notes"]
        for v in seen:
            if fmt_phone(v) not in notes:
                missing += 1
        for line in notes.split("\n"):
            if re.match(r"^\s*Phone\s+\d+\s*:", line):
                stale += 1
            if "OVERFLOW, not uploaded" in line or "CUT at phone cap" in line:
                m = phone_re.search(line)
                if m:
                    overflow_labeled.add((id(r), "".join(m.groups())))

    overflow_expected = stats["overflow_nums"]

    checks.append(("Output rows == input rows", len(rows), src_count, len(rows) == src_count))
    checks.append(("Phone slot gaps", gaps, 0, gaps == 0))
    checks.append(("Duplicate phone within record", dups, 0, dups == 0))
    checks.append(("Malformed phone values", bad, 0, bad == 0))
    checks.append(("Duplicate email within record", edups, 0, edups == 0))
    checks.append(("Rows missing property addr/ZIP", noprop, 0, noprop == 0))
    checks.append(("ALL-CAPS owner names", caps, 0, caps == 0))
    checks.append(("Non-5-digit ZIPs", badzip, 0, badzip == 0))
    checks.append(("Uploaded numbers absent from Notes", missing, 0, missing == 0))
    checks.append(("Stale 'Phone N :' slot refs", stale, 0, stale == 0))
    checks.append(("Unique overflow/cut numbers labeled", len(overflow_labeled),
                   overflow_expected, len(overflow_labeled) == overflow_expected))
    tv = stats.get("tier_violations", 0)
    checks.append(("Tier violations (worse # kept over better)", tv, 0, tv == 0))

    width = max(len(c[0]) for c in checks)
    print("\nVERIFICATION")
    for name, got, want, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}  got {got}  want {want}")
    passed = sum(1 for c in checks if c[3])
    print(f"\n  {passed}/{len(checks)} checks passed.")
    return passed == len(checks)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--stamp", default=date.today().strftime("%m/%Y"))
    ap.add_argument("--max-phones", type=int, default=DEFAULT_MAX_PHONES)
    ap.add_argument("--max-emails", type=int, default=DEFAULT_MAX_EMAILS)
    ap.add_argument("--trestle-results", action="append", default=[],
                    metavar="CSV",
                    help="Trestle phone_validator output (validation_results.csv "
                         "or phone_tags_for_datasift.csv; repeatable). When "
                         "given, records over the phone cap drop their worst-"
                         "tier numbers first instead of last-collected.")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    with open(a.input, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    required = ["Input Property Address", "ResultCode", "Phone1", "Relative1 Name"]
    if rows:
        miss = [c for c in required if c not in rows[0]]
        if miss:
            sys.exit(f"ERROR: not a DirectSkip contactinfo file — missing {miss}. "
                     "Do not proceed; columns would misalign.")

    trestle = load_trestle_results(a.trestle_results) if a.trestle_results else None
    cols, out, stats = process(rows, a.stamp, a.max_phones, a.max_emails,
                               trestle=trestle)

    with open(a.output, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)

    print(f"DirectSkip Clean — {a.stamp}")
    print(f"Source: {a.input}  ({len(rows)} rows)")
    print(f"Output: {a.output}  ({len(out)} rows, {len(cols)} cols)\n")
    print(f"  Phones uploaded         : {stats['phones']}")
    print(f"  Emails uploaded         : {stats['emails']}")
    print(f"  Records over {a.max_phones} slots  : {stats['overflow_recs']} "
          f"({stats['overflow_nums']} numbers in Notes, tagged)")
    if trestle is not None:
        print(f"  Trestle scores loaded   : {len(trestle)} numbers "
              f"from {len(a.trestle_results)} file(s)")
        by_tier = stats["dropped_by_tier"]
        if by_tier:
            order = {"invalid": 0, "Drop": 1, "Dial Fourth": 2, "unscored": 3,
                     "Dial Third": 4, "Dial Second": 5, "Dial First": 6}
            detail = ", ".join(f"{k} {v}" for k, v in
                               sorted(by_tier.items(),
                                      key=lambda kv: order.get(kv[0], 9)))
            print(f"  Tier-trimmed records    : {stats['trimmed_recs']} "
                  f"(cut: {detail})")
        else:
            print(f"  Tier-trimmed records    : 0 (no record exceeded the cap)")
    print(f"  Deceased owners         : {stats['deceased']}")
    print(f"  Low-confidence matches  : {stats['suspect']}")
    print(f"  Confirmed addr differs  : {stats['addr_diff']}")
    print(f"  Records with no phone   : {stats['no_phone']}")

    if a.verify and not verify(cols, out, len(rows), a.max_phones,
                               a.max_emails, stats):
        sys.exit("\nVerification FAILED — do not upload this file.")

    print("\nNEXT: map Notes + Owner Deceased by hand in step 4 of the wizard.")


if __name__ == "__main__":
    main()

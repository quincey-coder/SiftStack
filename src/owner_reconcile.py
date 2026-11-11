"""Owner reconcile — every record's owner checked against the parcel's owner of record.

Built 2026-11-11 from the manual audit that every upload batch needed. Across
the 09-18 .. 11-11 batches the operator hand-patched the same defects again and
again; this step makes the Actor's CSVs come out the way those patches left them:

* **Substitute trustees as owners** (Travis foreclosures: Kipp Marshall, Brandon
  Wolf, Christopher Baxter, Mackie Keller, ...). A blocklist only knows the
  names someone already added; comparing with the owner of record catches the
  next one too. Foreclosures and tax sales take the CAD owner whenever the
  filed name does not match it (the filed name stays in Notes).
* **Ownerless rows / no parcel id.** Travis addresses resolve to a parcel
  through the TCAD parcel fabric (``travis_gis.geo_id_by_situs``) — the tax
  cache's address index is keyed by MAILING address and returns a tenant's own
  parcel for the house they rent.
* **Liens / lis pendens on the wrong person's house.** The name backfill
  attaches a debtor to a same-surname neighbour, spouse, parent or near-miss
  name. A lien ships only when the debtor IS an owner of record (surname +
  first name, middle initials agree); otherwise it goes to the REVIEW file.
* **Reversed / particle names** ("Schaaf Diana", "Delden Van", "Alba De") are
  re-oriented from the owner of record when the debtor matched.
* **City glued onto the street** ("4611 Outpost Trace Lago Vista").
* **No house number** -> review (DataSift's validator can't place it).

Held records are not dropped silently: they are returned to the caller, which
writes ``datasift_REVIEW_<ts>.csv`` (Drive + the Slack CSV-links block). Set
``SIFT_SKIP_OWNER_RECONCILE=1`` to bypass the step entirely.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter

logger = logging.getLogger(__name__)

COUNTIES = ("travis", "bell", "williamson")
# Owner = owner of record whenever the filed name doesn't match it.
PROPERTY_KEYED = {"foreclosure", "tax_sale"}
# Owner taken from the owner of record only when the record has none.
FILL_BLANK = {"fire_damage", "code_violation"}
# Owner is the DEBTOR/DEFENDANT and must BE an owner of record.
DEBTOR_TYPES = {"lien", "lis_pendens"}
# Owner is a court party / the roll itself — never touched (probate PR,
# eviction landlord, divorce parties, the Austin BSC party, tax roll owner).
UNTOUCHED = {"probate", "eviction", "divorce", "condemned", "tax_delinquent"}

_NOISE = {
    "JR", "SR", "II", "III", "IV", "V", "ETUX", "ETAL", "ET", "AL", "UX", "VIR",
    "EST", "ESTATE", "OF", "THE", "AND", "MR", "MRS", "MS", "DR", "TRUST", "TRUSTEE",
    "TRUSTEES", "TR", "REVOCABLE", "LIVING", "FAMILY", "IRREVOCABLE", "LIFE",
    "DECD", "DECEASED", "AKA", "FKA", "NKA", "A/K/A",
}
_PARTICLES = {"VAN", "VON", "DE", "DEL", "DELA", "DELLA", "LA", "LE", "DI", "DA",
              "DOS", "DAS", "ST", "SAN", "SANTA", "MAC", "DU", "TER", "TEN", "VANDER"}
_HOSPITAL_RE = re.compile(r"\b(HOSPITAL|MEDICAL CENTER|HEALTH SYSTEM|HEALTHCARE SYSTEM)\b", re.I)
_ENTITY_GENERIC = {"LLC", "L", "C", "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LP",
                   "LTD", "LLP", "PLLC", "PC", "THE", "OF", "AND", "TRUST", "SERIES"}


# ── name parsing ─────────────────────────────────────────────────────

def _tokens(s: str) -> list[str]:
    s = (s or "").upper().replace("&", " & ")
    return [t for t in re.sub(r"[^A-Z0-9&,' -]", " ", s).replace(",", " , ").split()]


def _clean(toks: list[str]) -> list[str]:
    return [t.strip("'-") for t in toks if t not in _NOISE and t not in (",", "&") and t.strip("'-")]


def _surname_split(toks: list[str]) -> tuple[list[str], list[str]]:
    """NAMELF tokens -> (surname tokens, given tokens), joining particles."""
    if not toks:
        return [], []
    i = 1
    while i < len(toks) - 1 and toks[i - 1] in _PARTICLES:
        i += 1
    return toks[:i], toks[i:]


def cad_people(cad_raw: str) -> list[tuple[list[str], list[str]]]:
    """Every (surname, given-names) reading of a county owner string.

    Handles NAMELF (``RILEY GERALD S JR & GINA G``), comma form
    (``Norberg, Tyler S & Ashley N``), particle surnames (``VAN DELDEN JEFFREY``,
    ``DE BEST JOHN P``) and a FIRST-LAST co-owner after ``&`` (Bell:
    ``Noble, Lacy Denise & Jeffrey Hicks``)."""
    raw = (cad_raw or "").upper().strip().rstrip("&").strip()
    segs = [s.strip() for s in re.split(r"\s*&\s*|\s+AND\s+", raw) if s.strip()]
    out: list[tuple[list[str], list[str]]] = []
    first_surname: list[str] = []
    for i, seg in enumerate(segs):
        if "," in seg:
            last, rest = seg.split(",", 1)
            sur = _clean(_tokens(last))
            given = _clean(_tokens(rest))
            if sur:
                out.append((sur, given))
                if i == 0:
                    first_surname = sur
            continue
        toks = _clean(_tokens(seg))
        if not toks:
            continue
        if i == 0:
            sur, given = _surname_split(toks)
            first_surname = sur
            out.append((sur, given))
        else:
            if first_surname:
                out.append((first_surname, toks))           # "& JANNIECE" -> Janniece Kaelin
            if len(toks) >= 2:
                out.append(([toks[-1]], toks[:-1]))         # "& JEFFREY HICKS"
                out.append(_surname_split(toks))            # "& SMITH JANE"
    return out


def _compat(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a))


def _lev1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return any(l[:k] + l[k + 1:] == s for k in range(len(l)))


def person_matches(name: str, cad_raw: str) -> tuple[bool, dict | None]:
    """Is the person ``name`` (any word order) one of the owners in ``cad_raw``?

    Requires the owner's surname AND a compatible first name; when both carry a
    middle name/initial they must agree. Returns (True, {"first", "last"})
    oriented from the owner of record, else (False, None)."""
    d = _clean(_tokens(name))
    if not d:
        return False, None
    for sur, given in cad_people(cad_raw):
        if not sur or not given or not all(t in d for t in sur):
            continue
        rem = list(d)
        for t in sur:
            rem.remove(t)
        if not rem:
            continue
        df, cf = rem[0], given[0]
        first = df
        via_middle = False
        ok = _compat(df, cf)
        if not ok and len(rem) == 1 and len(given) >= 2 and any(
                len(g) > 1 and _compat(df, g) for g in given[1:]):
            ok, via_middle = True, True                  # SERNA ROGELIO / SERNA FRANCISCO ROGELIO
        if (not ok and len(df) >= 5 and _lev1(df, cf) and len(rem) >= 2 and len(given) >= 2
                and _compat(rem[1], given[1])):
            ok, first = True, cf                         # ORIRIS IVETH / OSIRIS IVETH (typo)
        if not ok:
            continue
        if not via_middle and len(rem) >= 2 and len(given) >= 2 and rem[1][0] != given[1][0]:
            continue                                     # JONES STEPHEN ADAM / JONES STEPHEN M
        return True, {"first": first.title(), "last": " ".join(sur).title()}
    return False, None


def entity_matches(name: str, cad_raw: str) -> bool:
    a = {t for t in _tokens(name) if t not in _ENTITY_GENERIC and t not in (",", "&")}
    b = {t for t in _tokens(cad_raw) if t not in _ENTITY_GENERIC and t not in (",", "&")}
    return bool(a and b) and (a <= b or b <= a)


def surname_overlap(name: str, cad_raw: str) -> bool:
    d = set(_clean(_tokens(name)))
    return any(sur and all(t in d for t in sur) for sur, _g in cad_people(cad_raw))


def owner_display(cad_raw: str) -> tuple[str, bool]:
    """Owner of record -> (display name, is_entity). Person = 'First Last'."""
    from notice_parser import _detect_entity_type
    raw = (cad_raw or "").strip()
    if not raw:
        return "", False
    if _detect_entity_type(raw) or _HOSPITAL_RE.search(raw):
        disp = re.sub(r"\s+THE$", "", raw.strip(), flags=re.I)
        return disp.title(), True
    people = cad_people(raw)
    if not people or not people[0][1]:
        return raw.title(), True       # unparseable -> never a fabricated person
    sur, given = people[0]
    return f"{given[0].title()} {' '.join(sur).title()}", False


# ── address helpers ──────────────────────────────────────────────────

def strip_glued_city(address: str, city: str) -> str:
    """'4611 Outpost Trace Lago Vista' + city 'Lago Vista' -> '4611 Outpost Trace'."""
    a, c = (address or "").strip(), (city or "").strip()
    if not a or not c or len(a) <= len(c) + 1:
        return a
    if a.upper().endswith(" " + c.upper()):
        head = a[: -len(c)].strip()
        if re.match(r"^\d+\s+\S+", head):
            return head
    return a


def title_address(street: str) -> str:
    """Title-case a mailing street without '4Th' / 'Ne' / 'Po Box' artifacts."""
    t = (street or "").strip().title()
    t = re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), t)
    t = re.sub(r"\b(Ne|Nw|Se|Sw)\b", lambda m: m.group(1).upper(), t)
    t = re.sub(r"\bPo Box\b", "PO Box", t)
    return t


def _mail_ok(street) -> bool:
    """A usable mailing street: has a number or a PO Box. County rolls put
    care-of / co-owner NAME lines in the mailing slot ("Natalie N Van Delden")."""
    s = (street or "").strip()
    return bool(s) and bool(re.search(r"\d|\bBOX\b", s, re.I))


def has_house_number(address: str) -> bool:
    return bool(re.match(r"^\s*\d", address or ""))


# ── CAD lookup ───────────────────────────────────────────────────────

def lookup_owner_of_record(n) -> dict | None:
    """Owner of record for the record's property: parcel first, then (Travis)
    the parcel fabric by situs, then a SITUS-keyed address index."""
    from cad_lookup import lookup_property_by_address, lookup_property_by_parcel
    county = (n.county or "").lower()
    res = None
    if (n.parcel_id or "").strip():
        try:
            res = lookup_property_by_parcel(n.parcel_id, county)
        except Exception as e:
            logger.debug("owner reconcile: parcel lookup %s failed: %s", n.parcel_id, e)
    if not res and county == "travis" and has_house_number(n.address):
        try:
            from travis_gis import geo_id_by_situs
            geo = geo_id_by_situs(n.address, n.zip)
        except Exception as e:
            logger.debug("owner reconcile: GIS lookup %r failed: %s", n.address, e)
            geo = None
        if geo:
            try:
                res = lookup_property_by_parcel(geo, "travis")
            except Exception:
                res = None
            if not res:
                return {"parcel_id": geo, "owner_raw": "", "source": "tcad_gis_no_owner"}
    if not res and has_house_number(n.address):
        try:
            res = lookup_property_by_address(n.address, county, zip_code=n.zip)
        except Exception as e:
            logger.debug("owner reconcile: address lookup %r failed: %s", n.address, e)
            res = None
        # Travis's address index is keyed by the owner's MAILING address: a hit
        # is whoever LIVES there, who may own a different parcel. Only a true
        # situs record (delinquent roll) is trusted.
        if res and county == "travis" and res.get("source") != "delinquent_situs":
            res = None
    return res


def _apply_owner_of_record(n, res: dict, *, replace_name: bool) -> None:
    cad_raw = (res.get("owner_raw") or "").strip()
    disp, is_entity = owner_display(cad_raw)
    if replace_name and (n.owner_name or "").strip():
        n.notice_party_name = (n.tax_owner_name or n.owner_name).strip()
    n.owner_name = disp
    n.tax_owner_name = cad_raw
    if not is_entity:
        n.entity_type = n.entity_person_name = n.entity_person_role = ""
    if res.get("parcel_id"):
        n.parcel_id = res["parcel_id"]
    if _mail_ok(res.get("mail_street")):
        n.owner_street = title_address(res["mail_street"])
        n.owner_city = (res.get("mail_city") or "").strip().title()
        n.owner_state = (res.get("mail_state") or "TX").strip().upper()
        n.owner_zip = (res.get("mail_zip") or "").strip()[:5]
    else:
        n.owner_street = n.owner_city = n.owner_state = n.owner_zip = ""


def _hold(n, reason: str, review: list, counts: Counter, tag: str) -> None:
    n.owner_check = "review"
    n.review_reason = reason
    review.append(n)
    counts[tag] += 1


def reconcile_owners(notices: list) -> tuple[list, list]:
    """Check every record's owner against the owner of record.

    Returns (kept, review). ``review`` records are held out of the upload CSVs
    with ``review_reason`` set; everything else is corrected in place."""
    if os.environ.get("SIFT_SKIP_OWNER_RECONCILE", "").strip() in ("1", "true", "yes"):
        logger.info("Owner reconcile skipped (SIFT_SKIP_OWNER_RECONCILE)")
        return notices, []
    kept, review = [], []
    counts: Counter = Counter()
    for n in notices:
        t = (n.notice_type or "").lower()
        county = (n.county or "").lower()
        if getattr(n, "record_status", "") in ("sold", "resolved") or county not in COUNTIES:
            kept.append(n)
            continue
        if n.address:
            n.address = re.sub(r"\s+", " ", n.address).strip()   # '16112   Viburnum Dr'
        if n.address and n.city:
            fixed = strip_glued_city(n.address, n.city)
            if fixed != n.address:
                logger.info("  street %r -> %r (city glued on)", n.address, fixed)
                n.address = fixed
                counts["street_city_fixed"] += 1
        if (n.address or "").strip() and not has_house_number(n.address):
            _hold(n, f"no house number in property address {n.address!r}", review, counts, "no_house_number")
            continue
        if t in UNTOUCHED or not (n.address or "").strip():
            kept.append(n)
            continue

        res = lookup_owner_of_record(n)
        cad_raw = ((res or {}).get("owner_raw") or "").strip()
        n.cad_owner_raw = cad_raw
        if res and res.get("parcel_id") and not (n.parcel_id or "").strip():
            n.parcel_id = res["parcel_id"]

        if t in DEBTOR_TYPES:
            debtor = (n.tax_owner_name or n.owner_name or "").strip()
            if _HOSPITAL_RE.search(debtor):
                _hold(n, f"debtor is a hospital ({debtor}) — not a homeowner lien", review, counts, "hospital_debtor")
                continue
            if not cad_raw:
                _hold(n, f"owner of record not found for {n.address} — debtor {debtor!r} unverified",
                      review, counts, "debtor_unverified")
                continue
            from notice_parser import _detect_entity_type
            if _detect_entity_type(debtor):
                if entity_matches(debtor, cad_raw):
                    n.owner_check = "match"
                    counts["debtor_match"] += 1
                    kept.append(n)
                else:
                    _hold(n, f"debtor {debtor!r} is not the owner of record {cad_raw!r}",
                          review, counts, "debtor_mismatch")
                continue
            ok, oriented = person_matches(debtor, cad_raw)
            if not ok:
                _hold(n, f"debtor {debtor!r} is not the owner of record {cad_raw!r}",
                      review, counts, "debtor_mismatch")
                continue
            new = f"{oriented['first']} {oriented['last']}"
            if new.lower() != (n.owner_name or "").lower():
                logger.info("  %s owner %r -> %r (oriented from owner of record %r)",
                            n.address, n.owner_name, new, cad_raw)
                counts["debtor_name_oriented"] += 1
                n.owner_name = new
            if _mail_ok(res.get("mail_street")) and not (n.owner_street or "").strip():
                _apply_mailing_only(n, res)
            n.owner_check = "match"
            counts["debtor_match"] += 1
            kept.append(n)
            continue

        if t in PROPERTY_KEYED or t in FILL_BLANK:
            filed = (n.tax_owner_name or n.owner_name or "").strip()
            if not cad_raw:
                n.owner_check = "unresolved" if not filed else "match"
                counts["owner_of_record_not_found"] += 1
                kept.append(n)
                continue
            if not filed:
                _apply_owner_of_record(n, res, replace_name=False)
                n.owner_check = "filled"
                counts["owner_filled"] += 1
            elif t in PROPERTY_KEYED and not (
                person_matches(filed, cad_raw)[0] or surname_overlap(filed, cad_raw)
                or entity_matches(filed, cad_raw)
            ):
                logger.info("  %s %s: filed name %r != owner of record %r -> owner of record",
                            t, n.address, filed, cad_raw)
                _apply_owner_of_record(n, res, replace_name=True)
                n.owner_check = "from_cad"
                counts["owner_replaced"] += 1
            else:
                n.owner_check = "match"
                counts["owner_match"] += 1
        kept.append(n)

    logger.info("Owner reconcile: kept %d, held for review %d — %s",
                len(kept), len(review), dict(counts))
    return kept, review


def _apply_mailing_only(n, res: dict) -> None:
    n.owner_street = title_address(res["mail_street"])
    n.owner_city = (res.get("mail_city") or "").strip().title()
    n.owner_state = (res.get("mail_state") or "TX").strip().upper()
    n.owner_zip = (res.get("mail_zip") or "").strip()[:5]

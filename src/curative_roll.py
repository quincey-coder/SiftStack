"""Fresh curative universe: the three county delinquent rolls, minus the DataSift account.

The first curative audit ran on the records already in DataSift. This builds
the same candidate records straight from the county rolls and drops every
property the account already holds, so the funnel (gate1 -> county ->
docket/probate -> verify -> obit -> clerk -> score) only researches parcels
nobody has touched.

Each roll has its own shape:
  Travis      TaxDelqOpenData.csv  - one row per account; NAMELF owner; situs
              number + name ("MILTON ST W") + property ZIP, no city; SINCE
              ('0000' = current roll only); CAD appraisal value on the row.
  Williamson  wilco_delinquent_latest.xlsx - one row per Quick Ref x tax year;
              carries CURRENT-year bills (not delinquent until Feb 1); NAMELF
              owner with double spaces; situs packs street + city + ZIP.
  Bell        BellCAD_Delinquent_Roll_Condensed_*.xlsx - one row per prop_id;
              'LAST, FIRST' owner; years_owed '2016-2021'; multi-line mailing
              cell. Stale since 2026-07-17, so every Bell parcel's balance is
              re-read live (GSA) at Gate 1 and a paid-off parcel fails there.

The account is matched on the situs address (house number + street core +
ZIP, directional/suffix order-insensitive - DataSift rewrites both) and on
the parcel id of every record the first audit pulled.

    python src/curative_roll.py build            -> data/curative_audit/fresh/candidates.json
    python src/curative_audit.py --set fresh <stage>
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

logger = logging.getLogger("curative_roll")

ROOT = Path(__file__).resolve().parent.parent
FRESH_DIR = ROOT / "data" / "curative_audit" / "fresh"
ACCOUNT_INDEX = ROOT / "data" / "curative_audit" / "account_index.json"
OLD_CANDIDATES = ROOT / "data" / "curative_audit" / "candidates.json"

TRAVIS_ROLL = ROOT / "data" / "travis_tax_cache" / "TaxDelqOpenData.csv"
WILCO_ROLL = ROOT / "data" / "wilco_tax_cache" / "wilco_delinquent_latest.xlsx"
BELL_ROLL = ROOT / "data" / "bell_tax_cache" / "BellCAD_Delinquent_Roll_Condensed_latest.xlsx"

# The owner's standard tax-delinquent filters (tax_delinquent_{travis,bell,wilco} + the
# pipeline's Step 8a condo filter): target ZIPs, 2+ years, $3,000+ owed, no condos (state
# code A4) or unit-style addresses, no government / HOA owners; companies are KEPT. One
# owner override (2026-10-30): vacant land (C, D, Bell E1) and commercial (F) are kept too.
# Personal property, mobile-home-only, utilities, minerals and inventory are never real estate.
_KEEP_CODES = re.compile(r"^[A-F]")
_CONDO_CODE = "A4"
_GOVT_HOA = re.compile(r"\b(CITY OF|COUNTY|STATE OF TEXAS|ISD|INDEPENDENT SCHOOL|UNITED STATES|HOUSING AUTHORITY|"
                       r"HOA|HOMEOWNERS|OWNERS ASS|PROPERTY OWNERS|ASSOCIATION|MUNICIPAL|UTILITY DISTRICT|MUD)\b",
                       re.I)
_SUFFIXES = {"JR", "SR", "II", "III", "IV"}
# Jordan John (curative training 2026-10-29): 2-4 years behind with $4K-$15K in arrears is the
# "smoking gun" - enough to say the owner may be dead or the family can't clear title, not enough
# to eat the equity. $4K is the base; above $15K is still kept (the equity gate decides).
MIN_DUE = 4000.0
MIN_YEARS = 2
_FAMILY_TRUST = re.compile(r"\b(TRUST|TR)\b(?!EE)", re.I)

_DIRS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST"}
_SUFFIX = {"ST", "STREET", "DR", "DRIVE", "RD", "ROAD", "LN", "LANE", "AVE", "AV", "AVENUE", "BLVD", "CT", "COURT",
           "CIR", "CIRCLE", "TRL", "TRAIL", "LOOP", "PKWY", "PARKWAY", "WAY", "PL", "PLACE", "CV", "COVE", "PATH",
           "RUN", "XING", "HWY", "TER", "TERRACE", "BND", "BEND", "PASS", "HOLW", "HOLLOW", "SQ", "PT", "TRCE",
           "RDG", "VW", "GLN", "CRK", "SPGS", "LNDG", "ROW", "WALK", "PLZ", "EXPY", "FWY"}
_UNIT = re.compile(r"\b(UNIT|APT|STE|SUITE|BLDG|TRLR|SPC|SP|LOT|#)\b.*$", re.I)


_ORDINAL_WORDS = {"FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5", "SIXTH": "6",
                  "SEVENTH": "7", "EIGHTH": "8", "NINTH": "9", "TENTH": "10", "ELEVENTH": "11", "TWELFTH": "12"}
# the same road, written per source: 'F M RD 812' / 'FM 812', 'U S HY 183 S' / 'US HIGHWAY 183',
# 'STATE HY 71 W' / 'SH 71', 'COUNTY ROAD 251' / 'CR 251'
_ROAD_ALIASES = ((r"\bF\s+M\b|\bFARM\s+(TO\s+MARKET\s+)?(ROAD\s+)?", "FM "), (r"\bU\s+S\b", "US"),
                 (r"\bR\s+R\b|\bRANCH\s+ROAD\b", "RR"), (r"\bCOUNTY\s+(ROAD|RD)\b", "CR"),
                 (r"\bSTATE\s+(HY|HWY|HIGHWAY)\b", "SH"), (r"\b(HY|HIGHWAY)\b", "HWY"))


def _street_core(s: str) -> list[str]:
    """Street tokens that identify the road: no direction, suffix, unit letter or ordinal ending."""
    s = _UNIT.sub("", (s or "").upper())
    for pat, to in _ROAD_ALIASES:
        s = re.sub(pat, to, s)
    out = []
    for t in re.findall(r"[A-Z0-9]+", s):
        t = _ORDINAL_WORDS.get(t, re.sub(r"^(\d+)(ST|ND|RD|TH)$", r"\1", t))   # the Travis roll: '5 ST E' = 5th St E
        if t not in _DIRS and t not in _SUFFIX:
            out.append(t)
    return [t for t in out if len(t) > 1 or t.isdigit()] or out


def addr_key(street: str, zip5: str = "") -> tuple[str, str, str] | None:
    """(house number, sorted street core, zip5) - None when there is no house number."""
    toks = re.findall(r"[A-Z0-9]+", _UNIT.sub("", (street or "").upper()))
    if not toks or not toks[0][0].isdigit():
        return None
    num = re.match(r"\d+", toks[0]).group(0)
    core = sorted(_street_core(" ".join(toks[1:])))
    if not core:
        return None
    return num, " ".join(core), (zip5 or "")[:5]


def _name_words(name: str) -> set[str]:
    return {t for t in re.findall(r"[A-Z]{3,}", (name or "").upper()) if t not in {"LLC", "THE", "AND", "INC", "ETAL"}}


def _same_area(a: str, b: str) -> bool:
    """DataSift can correct a ZIP on ingest, but '1205 W 5th St 78703' is not '1205 S 5th St 76504' in Temple."""
    return not a or not b or a[:3] == b[:3]


class Account:
    """What DataSift already holds: situs address keys + parcel ids from the first audit."""

    def __init__(self):
        idx = json.loads(ACCOUNT_INDEX.read_text(encoding="utf-8"))
        self.streets: dict = {}   # (number, core) -> ZIPs
        self.by_house: dict = {}  # (number, ZIP) -> street cores, to catch a misspelled street
        self.bare: dict = {}      # (core, ZIP) of records with no house number -> owner name words
        for r in idx.values():
            a = r.get("address") or {}
            z = (a.get("zip5") or a.get("postal_code") or "")[:5]
            k = addr_key(a.get("street") or "", z)
            if k:
                self.streets.setdefault(k[:2], set()).add(z)
                self.by_house.setdefault((k[0], z), []).append(set(k[1].split()))
            elif a.get("street"):
                o = r.get("owner") or {}
                self.bare.setdefault((" ".join(sorted(_street_core(a["street"]))), z), set()).update(
                    _name_words(f"{o.get('last_name') or ''} {o.get('company') or ''}"))
        old = json.loads(OLD_CANDIDATES.read_text(encoding="utf-8")) if OLD_CANDIDATES.exists() else {}
        self.apns = {_apn_key(r.get("county") or "", r.get("apn") or "") for r in old.values()}
        self.records = len(idx)

    def has(self, county: str, apn: str, street: str, zip5: str, owner_raw: str = "") -> str:
        if _apn_key(county, apn) in self.apns:
            return "parcel"
        z = (zip5 or "")[:5]
        k = addr_key(street, z)
        if k:
            if any(_same_area(z, x) for x in self.streets.get(k[:2], ())):
                return "address"
            toks = {t for t in k[1].split() if len(t) >= 4 or t.isdigit()}
            if any(toks & cores for cores in self.by_house.get((k[0], z), ())):
                return "address"
            return ""
        # no house number: a street holds many lots, so the owner has to match too
        # (Hickman Ave 78723 is one lot for Ledgestone and another for Berkman)
        owners = self.bare.get((" ".join(sorted(_street_core(street))), z))
        return "address + owner" if owners and owners & _name_words(owner_raw) else ""


def _apn_key(county: str, apn: str) -> str:
    a = (apn or "").strip().upper()
    if county == "Travis":
        d = re.sub(r"\D", "", a)
        return f"T:{d}" if len(d) == 14 else f"T:pid{int(d)}" if d else ""
    if county == "Bell":
        d = re.sub(r"\D", "", a)
        return f"B:{int(d)}" if d else ""
    return f"W:{a}" if a else ""


def _money(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "") or 0)
    except ValueError:
        return 0.0


# ── per-county roll readers -> uniform rows ──────────────────────────

def travis_rows(latest: int) -> list[dict]:
    import travis_tax_schema as schema  # noqa: PLC0415
    from scrapers.travis_texdel_cleaner import city_for_zip  # noqa: PLC0415
    out = []
    with open(TRAVIS_ROLL, encoding="utf-8-sig", errors="replace", newline="") as f:
        for raw in csv.DictReader(schema.sanitize_csv_lines(f, source=TRAVIS_ROLL.name)):
            r = schema.normalize_delq_row(raw)
            acct = re.sub(r"\D", "", r.get("Account #") or "")
            since = (r.get("1st Year Delinquent") or "").strip()
            first = int(since) if since.isdigit() and int(since) else None
            num, name, z = (r.get("Street Number") or "").strip(), (r.get("Street Name") or "").strip(), \
                (r.get("Property Zip") or "").strip()[:5]
            street = f"{num} {name}".strip() if num and num.strip("0") else name   # land: street name only
            mail = " ".join(x for x in (r.get("Address 1"), r.get("Address 2"), r.get("Address 3")) if x)
            out.append({
                "county": "Travis", "apn": acct, "code": (r.get("Property Type Code") or "").strip(),
                "street": street.title(), "city": city_for_zip(z) if z else "", "zip5": z,
                "owner_raw": (r.get("Owner Name") or "").strip(),
                "mailing": {"street": mail.title(), "city": (r.get("City") or "").title(),
                            "state": r.get("State") or "", "zip5": (r.get("Zip Code") or "")[:5]},
                "years": (latest - first + 1) if first else 1, "first_year": first or latest,
                "taxes_due": _money(r.get("Total Due")), "market": _money(r.get("Appraisal Value")) or None,
                "cause": (r.get("Cause #") or "").strip(), "bankruptcy": (r.get("Bankruptcy Number") or "").strip(),
            })
    return out


def wilco_rows(latest: int) -> list[dict]:
    import openpyxl  # noqa: PLC0415
    from scrapers.tax_delinquent_wilco import _split_situs  # noqa: PLC0415
    wb = openpyxl.load_workbook(WILCO_ROLL, read_only=True, data_only=True)
    ws = next(s for s in wb.worksheets if s.max_row and s.max_row > 1)
    rows = ws.iter_rows(values_only=True)
    col = {h: i for i, h in enumerate(next(rows)) if h is not None}
    by: dict[str, dict] = {}
    for row in rows:
        qr = str(row[col["Quick Ref"]] or "").strip().upper()
        if not qr.startswith("R"):
            continue   # M = mobile home only, P = business personal property
        try:
            yr = int(row[col["Tax Year"]])
        except (TypeError, ValueError):
            continue
        bal = _money(row[col["Fee Balance"]])
        rec = by.get(qr)
        if rec is None:
            # 'GEORGETOWN TX 78628': the state between city and ZIP defeats the city peel
            situs = re.sub(r"\s+TX(?=\s+\d{5})", "", str(row[col["Situs Address"]] or ""), flags=re.I)
            street, city, z = _split_situs(situs)
            rec = by[qr] = {
                "county": "Williamson", "apn": qr, "code": "",
                "street": street.title(), "city": city, "zip5": z,
                "owner_raw": " ".join(str(row[col["Owner Name"]] or "").split()),
                "mailing": {"street": " ".join(str(row[col[k]] or "") for k in ("Addr1", "Addr2")).strip().title(),
                            "city": str(row[col["City"]] or "").title(), "state": str(row[col["State"]] or ""),
                            "zip5": str(row[col["Zip"]] or "")[:5]},
                "delq_years": set(), "taxes_due": 0.0, "current_due": 0.0, "market": None,
            }
        if bal <= 0:
            continue
        if yr <= latest:   # a current-year bill is not delinquent until Feb 1 (Tax Code 31.02)
            rec["delq_years"].add(yr)
            rec["taxes_due"] += bal
        else:
            rec["current_due"] += bal
    wb.close()
    out = []
    for rec in by.values():
        ys = rec.pop("delq_years")
        if not ys:
            continue
        rec["first_year"] = min(ys)
        rec["years"] = latest - min(ys) + 1
        out.append(rec)
    return out


def bell_rows(latest: int) -> list[dict]:
    import openpyxl  # noqa: PLC0415
    from scrapers.tax_delinquent_bell import _parse_ownr_addr, _parse_years_owed  # noqa: PLC0415
    wb = openpyxl.load_workbook(BELL_ROLL, read_only=True, data_only=True)
    ws = next(s for s in wb.worksheets if s.max_row and s.max_row > 1)
    rows = ws.iter_rows(values_only=True)
    col = {h: i for i, h in enumerate(next(rows)) if h is not None}
    out = []
    for row in rows:
        if row[col["prop_type"]] != "Real Property":
            continue
        pid = str(int(row[col["prop_id"]]))
        first, n = _parse_years_owed(row[col["years_owed"]])
        mst, mcity, mstate, mzip, care_of, _ = _parse_ownr_addr(str(row[col["Ownr_Addr"]] or ""))
        out.append({
            "county": "Bell", "apn": pid, "code": str(row[col["state_cd"]] or "").strip(),
            "street": str(row[col["situs_address"]] or "").strip().title(),
            "city": str(row[col["situs_city"]] or "").title(), "zip5": str(row[col["situs_zip"]] or "")[:5],
            "owner_raw": " ".join(str(row[col["Ownr_Name"]] or "").split()),
            "mailing": {"street": mst.title(), "city": mcity.title(), "state": mstate, "zip5": mzip},
            "care_of": care_of,
            "years": (latest - int(first) + 1) if first else n, "first_year": int(first) if first else None,
            "taxes_due": _money(row[col["Base_Tax_Due"]]), "market": None,
        })
    wb.close()
    return out


# ── build ────────────────────────────────────────────────────────────

def _owner_fields(raw: str) -> dict:
    import curative_audit as ca  # noqa: PLC0415
    names = [] if ca.is_entity(raw) else ca.person_names(raw)
    if not names or _FAMILY_TRUST.search(raw):   # a trust owns it: Business Name, never a made-up person
        return {"owner_first": "", "owner_last": "", "owner_company": raw.title()}
    t = names[0].split()
    while len(t) > 2 and t[-1].upper().rstrip(".") in _SUFFIXES:   # 'Michael Mclaughlin Jr'
        t = t[:-1]
    return {"owner_first": " ".join(t[:-1]), "owner_last": t[-1], "owner_company": ""}


def build() -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import curative_audit as ca  # noqa: PLC0415
    latest = ca._latest_delq_year()
    acct = Account()
    logger.info("DataSift account: %d properties, %d parcel ids from the first audit", acct.records, len(acct.apns))
    bell_mkt = ca._indexed(ROOT / "data" / "bell_tax_cache" / "BellCAD_Appraisal_Data_Condensed_latest.xlsx",
                           ca.CACHE_DIR / "bell_market.json", ca._build_bell_market)
    from enrichment_pipeline import _UNIT_DESIGNATOR_RE  # noqa: PLC0415
    from scrapers.travis_texdel_cleaner import TARGET_ZIPS as TRAVIS_ZIPS  # noqa: PLC0415
    from zip_filter import load_target_zips  # noqa: PLC0415
    zips = {"Travis": set(TRAVIS_ZIPS), "Bell": set(load_target_zips()), "Williamson": set(load_target_zips())}
    stats: dict[str, Counter] = {}
    out: dict = {}
    for county, reader in (("Travis", travis_rows), ("Williamson", wilco_rows), ("Bell", bell_rows)):
        st = stats[county] = Counter()
        rows = reader(latest)
        st["on the roll"] = len(rows)
        for r in rows:
            raw = r["owner_raw"]
            estate = bool(ca.ESTATE_RE.search(raw)) and not ca.LIFE_ESTATE_RE.search(raw)
            if county == "Bell" and r["market"] is None:
                r["market"] = bell_mkt.get(r["apn"])
            if r["code"] and not _KEEP_CODES.match(r["code"]):
                st["personal property / mobile home / utility"] += 1
                continue
            if not r["street"]:
                st["no situs address at all"] += 1
                continue
            if r["zip5"] and r["zip5"] not in zips[county]:
                st["outside the target ZIPs"] += 1
                continue
            up = r["street"].upper()
            if r["code"] == _CONDO_CODE or "#" in up or _UNIT_DESIGNATOR_RE.search(up):
                st["condo / unit address"] += 1
                continue
            if r["taxes_due"] < MIN_DUE:
                st[f"owes under ${MIN_DUE:,.0f}"] += 1
                continue
            if r["years"] < MIN_YEARS:
                st[f"under {MIN_YEARS} years behind"] += 1
                continue
            if _GOVT_HOA.search(raw):
                st["government / HOA owner"] += 1
                continue
            if r["market"] and r["market"] * ca.AS_IS_FACTOR - r["taxes_due"] < ca.GATE_MIN:
                st["equity under $75K (roll value)"] += 1
                continue
            why = acct.has(county, r["apn"], r["street"], r["zip5"], raw)
            if why:
                st[f"already in DataSift ({why})"] += 1
                continue
            uid = f"roll:{county}:{r['apn']}"
            out[uid] = {
                "uuid": uid, "source": "county_roll", "county": county, "apn": r["apn"],
                "street": r["street"], "city": r["city"], "zip5": r["zip5"],
                **_owner_fields(raw), "owner_raw_roll": raw,
                "owner_deceased": None, "owner_age": None, "owner_mailing": r["mailing"],
                "secondary_owners": [], "care_of": r.get("care_of") or "",
                "signals": ["owner_name:estate"] if estate else [],
                "tax_lists": ["Tax Delinquent"], "lists": [], "tags": [], "notes": [], "custom_fields": {},
                "prop_code": r["code"], "no_house_number": addr_key(r["street"], r["zip5"]) is None,
                "tax_delinquent_value": r["taxes_due"], "year_behind_on_taxes": r["years"],
                "tax_delinquent_year": r.get("first_year"), "roll_market": r["market"],
                "roll_cause": r.get("cause") or "", "roll_bankruptcy": r.get("bankruptcy") or "",
                **{k: None for k in ca.DISTRESS_FIELDS if k not in ("tax_delinquent_year",)},
                "estimate_value": None, "equity_percent": None,
            }
            st["NEW - not in DataSift"] += 1
    FRESH_DIR.mkdir(parents=True, exist_ok=True)
    old = ca._load(FRESH_DIR / "candidates.json", {}) or {}
    for u, rec in out.items():   # keep earlier stage results (gate1 / value) on a rebuild
        for k in ("gate1", "value"):
            if k in (old.get(u) or {}):
                rec[k] = old[u][k]
    ca._save(FRESH_DIR / "candidates.json", out)
    ca._save(FRESH_DIR / "build_stats.json", {k: dict(v) for k, v in stats.items()})
    for county, st in stats.items():
        logger.info("%s: %s", county, ", ".join(f"{k} {v:,}" for k, v in st.items()))
    logger.info("fresh candidates: %d -> %s", len(out), FRESH_DIR / "candidates.json")
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.argv[1:] != ["build"]:
        sys.exit(__doc__)
    build()

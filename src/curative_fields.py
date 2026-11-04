"""DataSift custom-field values for one audited curative record.

NEW_FIELDS match the "Curative Intelligence" group (curative_fields.json) and
EXISTING_FIELDS match fields already on the account, letter for letter: the
wizard silently drops a column with no field of that exact name. Links are only
emitted where a fresh browser opens the page directly (tested 2026-10-31):
Travis/Williamson clerk documents and Williamson probate cases stop at a
disclaimer or search page, and WCAD returned 503. Which facts are trustworthy is
decided once, in curative_audit.assess; this module only formats what survived.
"""

import json
import re
from pathlib import Path

DEFINITIONS_PATH = Path(__file__).parent / "curative_fields.json"
NEW_FIELDS = [f["label"] for f in json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))["fields"]]
EXISTING_FIELDS = ["Owner Deceased", "Date of Death", "Decedent Name", "Obituary URL", "Probate Open Date",
                   "Personal Representative", "Attorney / Executor Name & Contact", "Other Owners / Heirs Names",
                   "Heir Count"]

_DEATH_PROOF = {"court/recorded": "Court / Recorded", "county roll estate label": "County Roll Estate Label",
                "obituary + memorial": "Obituary + Memorial", "obituary only": "Obituary Only",
                "memorial only": "Memorial Only", "none": "None"}
_CONFIRMED = ("court/recorded", "county roll estate label")
_FIELD_MAX = 250


def _defect_type(defect: str) -> str:
    return defect.split(":")[0].strip().title()


def _cad_link(county: str, apn: str, account: str) -> str:
    digits = re.sub(r"\D", "", apn)
    if county == "Bell" and digits:
        return f"https://belltx-search.gsacorp.io/parcel/{digits.zfill(10)}"
    if county == "Travis" and digits and len(digits) <= 7:   # 14 digits is the Tax Office account, not the TCAD PID
        return f"https://travis.prodigycad.com/property-detail/{int(digits)}"
    return ""


def _tax_link(county: str, apn: str, account: str) -> str:
    digits = re.sub(r"\D", "", apn)
    if county == "Bell" and digits:
        return f"https://belltx-search.gsacorp.io/tax/{digits.zfill(10)}"
    if county == "Williamson" and apn:
        return f"https://tax.wilcotx.gov/Property-Detail?PropertyQuickRefID={apn.strip().upper()}"
    acct = re.sub(r"\D", "", account or "")
    if county == "Travis" and len(acct) == 14:
        return f"https://travis.go2gov.net/showPropertyInfo.do?account={acct}"
    return ""


def _letters_date(case: dict) -> str:
    if case.get("letters_date"):
        return case["letters_date"]
    for e in case.get("events") or []:
        if re.search(r"\bLetters?\b", e) and re.match(r"\d{4}-\d{2}-\d{2} ", e) and "Issue" in e:
            return e[:10]
    return ""


def links(rec: dict, a: dict) -> dict:
    """{field label: URL} for the pages that open directly. Custom fields show these as plain text;
    Notes turns them into clickable links, so both places carry them."""
    county, apn = rec.get("county") or "", str(rec.get("apn") or "")
    account = str((rec.get("value") or {}).get("account") or "")
    case = next(iter(a.get("probate") or []), {})
    probate = case.get("url") or ""
    out = {"CAD Link": _cad_link(county, apn, account),
           "Tax Link": _tax_link(county, apn, account),
           "Probate Link": probate if county == "Bell" and probate.startswith("http") else "",
           "Grave Link": next((f"https://www.findagrave.com/memorial/{m['id']}" for m in a.get("memorials") or []), "")}
    return {k: v for k, v in out.items() if v}


def _decedent(a: dict) -> str:
    dead = a.get("dead") or []
    if not dead:
        return ""
    # one person often appears twice ('James Lee', 'James D Lee'): take the fullest spelling
    last = dead[0]["name"].split()[-1].lower()
    name = max((d["name"] for d in dead if last in d["name"].lower().split()), key=len)
    name = re.sub(r"\s+Tr\b.*$", "", name)
    return re.sub(r"^(.+?)\s+(Jr|Sr|II|III|IV)\s+(\S+)$", r"\1 \3 \2", name)


def values(rec: dict, a: dict, obit: dict | None = None) -> dict:
    """{CSV header: value} for every field. Blank means nothing to say, so no value is written."""
    obit = obit or {}
    proof = a.get("death_proof") or "none"
    ghosts = a.get("ghosts") or []
    strengths = {(g.get("strength") or "").split(" ")[0] for g in ghosts}
    payer = next((p for p in a.get("payments") or [] if p.get("payer") and not p["payer"].startswith("?")), {})
    liens = a.get("clerk_open") or []
    case = next(iter(a.get("probate") or []), {})
    heirs = [h["name"] for h in a.get("heirs") or [] if len((h.get("name") or "").split()) >= 2]
    obit_out = any(r.startswith(("obituary ruled out", "undated obituary")) for r in a.get("ruled_out") or [])
    attorney = " ".join(x for x in (case.get("attorney_name"), case.get("attorney_phone")) if x)
    urls = links(rec, a)
    out = {
        "Curative Tier": a.get("tier") or "",
        "Curative Score": a.get("score") or "",
        "Curative Spread": a.get("spread") if a.get("spread") is not None else "",
        "Death Proof": _DEATH_PROOF.get(proof, ""),
        "Title Defect Type": "; ".join(dict.fromkeys(_defect_type(d) for d in a.get("defects") or []))[:_FIELD_MAX],
        "Ghost Payer Strength": ("Strong" if "strong" in strengths else "Weak") if ghosts else "None",
        "Last Tax Payer": payer.get("payer") or "",
        "Last Tax Payment Date": payer.get("date") or "",
        "Open Lien Creditors": "; ".join(
            f"{x.get('class') or x.get('doc_type')}: {x.get('creditor') or '?'} #{x.get('instrument')}"
            + (" (name match only)" if x.get("on_property") == "unknown" else "") for x in liens),
        "Legal Description": (a.get("legal") or "")[:_FIELD_MAX],
        "Letters Granted Date": _letters_date(case),
        "Last Court Activity": case.get("last_event_date") or "",
        "CAD Link": urls.get("CAD Link", ""),
        "Tax Link": urls.get("Tax Link", ""),
        "Probate Link": urls.get("Probate Link", ""),
        "Grave Link": urls.get("Grave Link", ""),
        # the owner of record, not whoever died earlier in the chain: Marian Crofford is alive, Ira is the decedent
        "Owner Deceased": ("Yes" if proof in _CONFIRMED else "Suspected") if a.get("owner_dead") else "",
        "Date of Death": case.get("date_of_death") or a.get("dod") or "",
        "Decedent Name": _decedent(a),
        "Obituary URL": "" if obit_out else obit.get("obituary_url") or "",
        "Probate Open Date": case.get("filed") or "",
        "Personal Representative": case.get("pr_name") or "",
        "Attorney / Executor Name & Contact": attorney,
        "Other Owners / Heirs Names": "; ".join(heirs)[:_FIELD_MAX],
        "Heir Count": len(heirs) or "",
    }
    return {k: out[k] for k in NEW_FIELDS + EXISTING_FIELDS}

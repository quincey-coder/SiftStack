"""Curative-title audit: tax-delinquent records with a deceased owner.

Operator-run CLI (NOT in the Actor). Finds tax-delinquent records in DataSift
whose owner is (probably) dead, then does the free research a VA would do:

  pull     DataSift (READ-ONLY) -> tax lists x deceased signals, hydrated
           with APN, owner.deceased, obituary date and the record's Notes.
  county   per-parcel county research, cached per parcel:
             Travis      go2gov receipts (who PAID) + TCAD Prodigy deeds
             Williamson  tax.wilcotx.gov Property-Detail (deeds + payor ids,
                         receipt PDFs only for non-owner payments)
             Bell        GSA parcel deed history + per-year paid/balance
                         (no payer online)
  probate  Williamson PublicAccess docket, searched by the name of everyone
           on title: estate cases, plus tax suits, divorces, heirship and title
           suits (Travis/Bell dockets are reCAPTCHA portals; their recorded
           AOH / executor deeds show up in the deed history instead)
  obit     obituary search (free web search) -> grounded death evidence
  verify   Find A Grave (Texas memorials) per person on title + the birth
           year on each matched obituary, checked against DataSift's age
  ancestry logged-in Ancestry: death indexes (birth-year gated) + the free
           1950 census household (parents/siblings = heir leads)
  score    classify payers + score + ranked report + DataSift Notes CSV

Nothing here spends money or writes to DataSift. The upload CSV goes through
the wizard; skip tracing the heirs is a separate, approval-gated step
(skip_orchestrator). Every stage caches under data/curative_audit/ so a
re-run only fetches what is new.

    python src/curative_audit.py pull
    python src/curative_audit.py county [--only Travis] [--limit N]
    python src/curative_audit.py probate
    python src/curative_audit.py obit
    python src/curative_audit.py verify
    python src/curative_audit.py ancestry [--limit N]   (headed browser, 100 pages/day)
    python src/curative_audit.py score
    python src/curative_audit.py all
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("curative_audit")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "curative_audit"
CACHE_DIR = DATA_DIR / "cache"
CANDIDATES = DATA_DIR / "candidates.json"
OUT_PREFIX = "curative_audit"
# --set fresh: the county-roll universe minus the DataSift account (curative_roll.py);
# the research caches are shared, so a parcel / name searched once is never searched again
SETS = {"account": (DATA_DIR / "candidates.json", "curative_audit"),
        "fresh": (DATA_DIR / "fresh" / "candidates.json", "curative_audit_FRESH")}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

BELL_CITIES = {"temple", "killeen", "belton", "harker heights", "nolanville",
               "salado", "troy", "holland", "rogers", "little river-academy",
               "little river academy", "morgan's point resort", "fort hood",
               "fort cavazos", "academy", "moody", "bartlett"}
WILCO_CITIES = {"round rock", "georgetown", "leander", "cedar park", "hutto",
                "taylor", "liberty hill", "jarrell", "florence", "granger",
                "thrall", "coupland", "weir", "bartlett"}

TAX_LISTS = ("Tax Delinquent", "Tax Sale")
# strong = reliable when checked (2026-10-21 top-25 audit); weak = name-only
STRONG_LISTS = ("Pre-Probate/Deceased",)
WEAK_LISTS = ("Estate and Heirs", "Heirs", "Inherited", "Probate", "Obituary")
STRONG_TAGS = ("deceased", "skip2 deceased")
WEAK_TAGS = ("has_heirs",)

# "LIFE ESTATE" / "LIFE EST" is a living life tenant, NOT a dead owner.
ESTATE_RE = re.compile(r"(?<!LIFE )(?<!REAL )\b(EST|ESTATE|ESTATE OF|DECEASED|DEC'D|DECD|HEIRS?)\b(?!\s+(?:HOLDINGS|PARTNERS|GROUP|LLC|INVEST))", re.I)
LIFE_ESTATE_RE = re.compile(r"\bLIFE\s+EST(ATE)?\b", re.I)
_ENTITY_EXTRA_RE = re.compile(r"\b(LLC|L\.?L\.?C|LP|L\.?P|LTD|INC|CORP|CORPORATION|COMPANY|FUND|PARTNERS(HIP)?|"
                              r"HOLDINGS|INVESTMENTS?|PROPERTIES|BANK|CHURCH|MINISTRIES|ASSOCIATION|LAND\s+TRUST)\b",
                              re.I)


# ── small io helpers ─────────────────────────────────────────────────

def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def _load(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def county_for_city(city: str, zip5: str = "", apn: str = "") -> str:
    # Williamson quick-ref ids are "R" + digits; Bartlett straddles Bell and
    # Williamson, so the APN shape beats the city.
    if re.fullmatch(r"[Rr]\d{4,}", (apn or "").strip()):
        return "Williamson"
    # a 14-digit Tax Office account is Travis: Leander, Cedar Park and Round Rock straddle the line
    if re.fullmatch(r"\d{14}", (apn or "").strip()):
        return "Travis"
    c = (city or "").strip().lower()
    if c in BELL_CITIES or zip5.startswith("765"):
        return "Bell"
    if c in WILCO_CITIES:
        return "Williamson"
    return "Travis"


# ── stage 1: pull (DataSift, read-only) ──────────────────────────────

def _ds_call(fn, *a, **k):
    from datasift_api_client import DataSiftAPIError  # noqa: PLC0415
    for _ in range(10):
        time.sleep(0.2)
        try:
            return fn(*a, **k)
        except (ConnectionError, OSError) as e:   # requests' ConnectionError subclasses OSError
            logger.warning("DataSift connection error (%s) - retrying", e.__class__.__name__)
            time.sleep(10)
            continue
        except DataSiftAPIError as e:
            if e.status == 429:
                m = re.search(r"available in (\d+)", e.body or "")
                time.sleep(min((int(m.group(1)) if m else 60) + 3, 300))
                continue
            if e.status == 404:
                return None
            if e.status >= 500:
                time.sleep(5)
                continue
            raise
    raise RuntimeError("DataSift retries exhausted")


def _ds_sweep(c, must: dict) -> dict:
    rows, off = {}, 0
    total = int(_ds_call(c.search_records, must, limit=1).get("count", 0))
    while off < total:
        page = (_ds_call(c.search_records, must, limit=100, offset=off) or {}).get("results") or []
        for r in page:
            rows[r["uuid"]] = r
        off += 100
        if not page:
            break
    return rows


def _custom_field_options(c) -> dict:
    opts = {}
    r = _ds_call(c._request_json, "GET", "/api/internal/custom-fields/?limit=999") or {}
    for f in (r.get("results") or r.get("data") or []):
        for o in (f.get("options") or []):
            if o.get("uuid"):
                opts[o["uuid"]] = o.get("label") or o.get("title") or o.get("value")
    return opts


DISTRESS_FIELDS = ("open_mortgages", "loan_to_value", "mortgage_type", "lien_type", "lien_recording_date",
                   "foreclosure_date", "auction_date", "bankruptcy_recording_date", "divorce_file_date",
                   "probate_open_date", "last_sale_price", "last_sold", "total_taxes", "tax_delinquent_year",
                   "tax_auction_date")


def _hydrate(c, uuid: str, opts: dict, light: bool = False) -> dict:
    """Record detail (1 GET). light=True skips the custom-field + Notes GETs (Gate 1 screens
    thousands of records; the heavy fields are fetched again for Gate 1 passes)."""
    d = _ds_call(c._request_json, "GET", f"/api/internal/property/{uuid}/") or {}
    o = d.get("owner") or {}
    addr = d.get("address") or {}
    rec = {
        "uuid": uuid,
        "street": addr.get("street") or "",
        "city": addr.get("city") or "",
        "zip5": addr.get("zip5") or (addr.get("postal_code") or "")[:5],
        "apn": d.get("apn") or d.get("parcel_id") or "",
        "owner_first": o.get("first_name") or "",
        "owner_last": o.get("last_name") or "",
        "owner_company": o.get("company") or "",
        "owner_deceased": o.get("deceased"),
        "owner_age": o.get("age"),
        "owner_mailing": o.get("address") or {},
        "secondary_owners": [
            {k: s.get(k) for k in ("first_name", "last_name", "company", "deceased")}
            for s in (d.get("secondary_owners") or [])
        ],
        "last_obituary_date": d.get("last_obituary_date"),
        "owned_since": d.get("owned_since"),
        "estimate_value": d.get("estimate_value"),
        "equity_percent": d.get("equity_percent"),
        "tax_delinquent_value": d.get("tax_delinquent_value"),
        "year_behind_on_taxes": d.get("year_behind_on_taxes"),
        "status": d.get("status"),
        "skiptraced": o.get("skiptraced"),
        "total_phones": o.get("total_phones"),
        # Gate 1 arrears + distress (DataSift / SiftMap enrichment)
        **{k: d.get(k) for k in DISTRESS_FIELDS},
        "lists": [x if isinstance(x, str) else (x or {}).get("title", "") for x in d.get("lists") or []],
        "tags": [x if isinstance(x, str) else (x or {}).get("title", "") for x in d.get("tags") or []],
    }
    if light:
        return rec
    cf = _ds_call(c._request_json, "GET", f"/api/internal/property/{uuid}/custom-field/") or {}
    vals = {}
    items = cf.get("results") or cf.get("data") or (cf if isinstance(cf, list) else [])
    for x in items:
        meta = x.get("custom_field") or {}
        v = x.get("value")
        if meta.get("field_type") == "select":
            v = opts.get(v, v)
        if v not in (None, "", []):
            vals[meta.get("label")] = v
    rec["custom_fields"] = vals
    msgs = _ds_call(c._request_json, "GET", f"/api/internal/property/{uuid}/message/?limit=20") or {}
    rec["notes"] = [
        (m.get("text") or m.get("message") or m.get("body") or "")[:6000]
        for m in (msgs.get("results") or msgs.get("data") or [])
    ]
    return rec


def stage_pull(refresh: bool = False, all_tax: bool = True) -> dict:
    """Every tax-delinquent / tax-sale record (Gate 1 universe), tagged with its deceased signals."""
    from datasift_api_client import DataSiftAPIClient  # noqa: PLC0415

    existing = {} if refresh else (_load(CANDIDATES, {}) or {})
    c = DataSiftAPIClient.from_env()
    lists = {l["title"]: l["uuid"] for l in _ds_call(c.list_lists, limit=999)}
    tags = {t["title"]: t["uuid"] for t in _ds_call(c.list_tags, limit=999)}

    tax: dict = {}
    tax_lists: dict[str, list[str]] = {}   # uuid -> established tax list(s) it sits on
    for title, lid in lists.items():
        if not any(title == x or (title.startswith("FTM ") and x in title) for x in TAX_LISTS):
            continue
        rows = _ds_sweep(c, {"any_lists": [lid]})
        tax.update(rows)
        for u in rows:
            tax_lists.setdefault(u, []).append(title)
    logger.info("tax-list records: %d", len(tax))

    signals: dict[str, list[str]] = {}

    def mark(rows: dict, label: str) -> None:
        for u in rows:
            if u in tax:
                signals.setdefault(u, []).append(label)

    for name in STRONG_LISTS + WEAK_LISTS:
        if name in lists:
            mark(_ds_sweep(c, {"any_lists": [lists[name]]}), f"list:{name}")
    for name in STRONG_TAGS + WEAK_TAGS:
        if name in tags:
            mark(_ds_sweep(c, {"any_tags": [tags[name]]}), f"tag:{name}")
    # The county roll's own "Est" owner label is a signal even with no list.
    for u, r in tax.items():
        ow = r.get("owner") or {}
        nm = " ".join(filter(None, [ow.get("company"), ow.get("first_name"), ow.get("last_name")]))
        if ESTATE_RE.search(nm or ""):
            signals.setdefault(u, []).append("owner_name:estate")

    logger.info("candidates with a deceased signal: %d", len(signals))
    opts = _custom_field_options(c)
    out = {}
    # Gate 1 screens EVERY tax record (equity first, curative problems second);
    # the deceased signals ride along as a Gate 2 input.
    universe = list(tax) if all_tax else list(signals)
    for i, u in enumerate(universe, 1):
        old = existing.get(u) or {}
        rec = old if old.get("street") and "open_mortgages" in old else None
        if rec is None:
            rec = _hydrate(c, u, opts, light=not old.get("street"))
            for k in ("custom_fields", "notes"):   # keep what an earlier full hydrate fetched
                if k in old:
                    rec[k] = old[k]
        rec["signals"] = sorted(set(signals.get(u, [])))
        rec["tax_lists"] = sorted(tax_lists.get(u, []))
        rec["county"] = county_for_city(rec["city"], rec["zip5"], rec["apn"])
        out[u] = rec
        if i % 100 == 0:
            logger.info("  hydrated %d/%d", i, len(universe))
            _save(CANDIDATES, out)
    _save(CANDIDATES, out)
    logger.info("saved %d candidates -> %s", len(out), CANDIDATES)
    return out


# ── name helpers ─────────────────────────────────────────────────────

_NAME_NOISE = {"ETUX", "ETVIR", "ETAL", "ET", "AL", "UX", "VIR", "EST", "ESTATE", "OF",
               "THE", "AND", "TOD", "LIFE", "JR", "SR", "II", "III", "IV", "DECD",
               "DECEASED", "HEIRS", "HEIR", "MRS", "MR", "DR", "TR", "TRUSTEE"}


def to_namelf(raw: str) -> str:
    """County owner/grantee text -> plain NAMELF for parse_tax_owner_name.

    'HALL, JAMES KEITH & RUTH ANN' -> 'HALL JAMES KEITH & RUTH ANN';
    '(TOD)' / '(O1495432)' / leading 'ESTATE OF' / trailing 'EST' dropped.
    Orientation is never flipped here: these sources are all NAMELF.
    """
    s = re.sub(r"\([^)]*\)", " ", raw or "")
    s = re.sub(r"^\s*(THE\s+)?ESTATE\s+OF\s*", "", s, flags=re.I)
    s = re.sub(r"\bESTATE\s+OF\s*$", " ", s, flags=re.I)   # 'BARBOSA YOLANDA ESTATE OF'
    s = re.sub(r"^\s*THE\s+", "", s, flags=re.I)
    s = LIFE_ESTATE_RE.sub(" ", s)
    s = re.sub(r"\b(EST|ESTATE|DECD|DECEASED|ET\s*AL|ETUX)\b\.?", " ", s, flags=re.I)
    s = re.sub(r"^([A-Z'\-]+),\s*", r"\1 ", s.strip(), flags=re.I)
    return " ".join(s.replace(",", " ").split())


_ESTATE_OF_SEG_RE = re.compile(r"(?:^|&)\s*(?:THE\s+)?ESTATE\s+OF\s*(?=\S)([^&]+)", re.I)
# 'LOPEZ, THE ESTATE OF NAT' = the estate of Nat Lopez
_LAST_ESTATE_OF_RE = re.compile(r"^\s*([A-Z'\-]+),?\s+THE\s+ESTATE\s+OF\s+(.+)$", re.I)
# 'THE ANNIE ALTA BASS ESTATE' is prose order (FIRST ... LAST)
_THE_X_ESTATE_RE = re.compile(r"^\s*THE\s+(.+?)\s+EST(?:ATE)?\s*$", re.I)


def _prose_name(s: str) -> str:
    return " ".join(re.sub(r"\([^)]*\)|[,.]|\bDECEASED\b|\bDEC'?D\b", " ", s, flags=re.I).split()).title()


def _amp(raw: str) -> str:
    """'MCLAUGHLIN SHANE & MENDY and HERMAN & ...' -> '... & MENDY & HERMAN & ...'.
    Entities keep their AND ('SMITH AND SONS LLC')."""
    raw = raw or ""
    return raw if is_entity(raw) else re.sub(r"\s+AND\s+", " & ", raw, flags=re.I)


# a later co-owner written prose order with a middle initial: 'THELMA A MCGARRAH'
_PROSE_MI_RE = re.compile(r"^[A-Z'\-]{2,}\s+[A-Z]\.?\s+[A-Z'\-]{2,}$", re.I)


# 'VEDDER FAMILY TRUST' / 'PEREZ ADOLFO LIVING TRUST' / 'PHONG & KHUYEN REVOCABLE LIVING TR':
# the trust words are not a name ('Family Vedder'); what is left is the settlor, if anyone
_TRUST_TAIL_RE = re.compile(r"\s+(?:THE\s+)?(?:(?:FAMILY|LIVING|REVOCABLE|IRREVOCABLE|SURVIVORS?'?S?|JOINT|"
                            r"MANAGEMENT|DECLARATION\s+OF)\s+)*(?:TRUST|TR)\b.*$", re.I)
_TRUSTEE_RE = re.compile(r"\s+(?:CO-?)?TRUSTEES?\b(?:\s+(?:FOR|OF))?", re.I)


def strip_trust(raw: str) -> str:
    return _TRUSTEE_RE.sub(" ", _TRUST_TAIL_RE.sub("", raw or "")).strip()


def owner_segments(raw: str) -> list[tuple[str, bool]]:
    """County owner/grantee text -> [('First Last', estate_labeled), ...].

    The roll is NAMELF, but the estate parts are often written as prose:
    'ESTATE OF <FIRST LAST>', 'THE <FIRST LAST> ESTATE', '<LAST>, THE ESTATE
    OF <FIRST>', and - when the first owner is written 'LAST, FIRST' - a later
    comma-less '<FIRST LAST> ESTATE' segment. Those are taken as-is; the rest
    goes through the shared NAMELF parser.
    """
    from obituary_enricher import parse_tax_owner_name  # noqa: PLC0415
    raw = strip_trust(_amp(raw))
    segs = [s.strip() for s in re.split(r"\s*&\s*", raw) if s.strip()]
    out: list[tuple[str, bool]] = []
    prose: list[tuple[str, bool]] = []   # after the NAMELF owner, who is the primary
    namelf: list[str] = []
    comma_style = bool(segs) and "," in segs[0]
    for i, seg in enumerate(segs):
        m = (_ESTATE_OF_SEG_RE.match(seg) or _THE_X_ESTATE_RE.match(seg))
        nm = ""
        if m:
            nm = _prose_name(m.group(1))
        elif (m := _LAST_ESTATE_OF_RE.match(seg)):
            nm = _prose_name(f"{m.group(2)} {m.group(1)}")
        elif i > 0 and comma_style and "," not in seg and ESTATE_RE.search(seg) and len(seg.split()) >= 3:
            nm = _prose_name(re.sub(r"\bEST(ATE)?\b", " ", seg, flags=re.I))
        elif i > 0 and _PROSE_MI_RE.match(seg):
            prose.append((_prose_name(seg), False))
            continue
        if m or nm:
            if len(nm.split()) >= 2 and not is_entity(nm):
                out.append((nm, True))
            continue
        namelf.append(seg)
    s = to_namelf(" & ".join(namelf))
    if s and not s.startswith("*") and not is_entity(s):
        est_segs = [g for g in namelf if ESTATE_RE.search(g)]
        for n in parse_tax_owner_name(s):
            if len(n.split()) >= 2 and n not in [o[0] for o in out]:
                out.append((n, any(_same_person(n, g) for g in est_segs)))
    return out + [x for x in prose if x[0] not in [o[0] for o in out]]


def person_names(raw: str) -> list[str]:
    """NAMELF owner text -> ['First Last', ...]; [] for entities/blank."""
    raw = strip_trust(_amp(raw))
    names = [n for n, _ in owner_segments(raw)]
    rest = " & ".join(s for s in re.split(r"\s*&\s*", raw)
                      if s.strip() and not ESTATE_RE.search(s) and not _PROSE_MI_RE.match(s))
    s = to_namelf(rest)
    if not s or s.startswith("*") or is_entity(s):
        return names
    # "HALL JAMES KEITH & RUTH ANN": the shared parser reads a 2-token co-owner
    # as LAST FIRST ("Ann Ruth"); it is just as often FIRST MIDDLE sharing the
    # surname. Searching is cheap, so try that reading too (never replace).
    parts = [p.split() for p in re.split(r"\s*&\s*", s)]
    if len(parts) > 1 and len(parts[0]) >= 2:
        for tail in parts[1:]:
            if len(tail) == 2:
                alt = f"{tail[0].title()} {parts[0][0].title()}"
                if alt not in names:
                    names.append(alt)
    extra = extra_readings(raw)
    if extra:
        # the shared parser splits particle / two-surname names into nonsense ('La De', 'Lopez Garcia',
        # 'G Matilde'): drop its readings that start with a surname, a particle or an initial
        surnames = {t for n in extra for t in n.upper().split()[1:]}
        keep = []
        for n in names:
            t = n.upper().split()
            if t[0] in NAME_PARTICLES or len(t[0]) == 1 or len(t[-1]) == 1 or t[-1] in NAME_PARTICLES or \
                    (t[0] in surnames and t[0] not in {x.upper().split()[0] for x in extra}):
                continue
            keep.append(n)
        names = keep
    for n in extra:
        if n not in names:
            names.append(n)
    return names


# ── Hispanic + particle surnames ─────────────────────────────────────
# 'GARCIA LOPEZ MARIA DEL CARMEN' = Maria del Carmen, surnames Garcia (father) + Lopez (mother).
# Records name her either way - 'Maria Garcia Lopez', 'Maria Garcia', sometimes 'Maria Lopez' -
# so we read both surnames and match on either.

NAME_PARTICLES = {"DE", "DEL", "LA", "LAS", "LOS", "DA", "DAS", "DO", "DOS", "VAN", "VON", "DER", "DI", "Y"}
HISPANIC_SURNAMES = set("""
GARCIA HERNANDEZ MARTINEZ LOPEZ GONZALEZ RODRIGUEZ PEREZ SANCHEZ RAMIREZ TORRES FLORES RIVERA GOMEZ DIAZ
REYES CRUZ MORALES ORTIZ GUTIERREZ CHAVEZ RAMOS RUIZ ALVAREZ MENDOZA CASTILLO JIMENEZ VASQUEZ MORENO HERRERA
MEDINA AGUILAR GARZA CASTRO VARGAS FERNANDEZ GUZMAN MUNOZ MENDEZ SALAZAR SOTO DELGADO PENA RIOS ALVARADO
SANDOVAL CONTRERAS VALDEZ GUERRERO ORTEGA ESTRADA NUNEZ MALDONADO VEGA VAZQUEZ SANTIAGO DOMINGUEZ ESPINOZA
SILVA PADILLA MARQUEZ CORTEZ ROJAS ACOSTA FIGUEROA LUNA JUAREZ NAVARRO CAMPOS MOLINA AVILA AYALA MEJIA
CARRILLO DURAN CABALLERO TREVINO CANTU VILLARREAL SALINAS RANGEL ZAMORA FUENTES IBARRA LOZANO CERVANTES SOLIS
LEAL GALVAN OCHOA ROSALES CARDENAS VELASQUEZ PACHECO MONTOYA BARRERA ESCOBAR ARREDONDO SAENZ TIJERINA
ELIZONDO ZUNIGA BENAVIDES HINOJOSA OLIVARES ROCHA SERNA YBARRA SOSA ESPARZA PONCE VELA RUBIO QUINTERO SANTOS
ROBLES BAUTISTA CALDERON LARA CISNEROS ARELLANO CORONA MATA MIRELES GALLEGOS PALACIOS BERNAL OROZCO VALENCIA
NAVA MONTES ESCAMILLA AGUIRRE VELAZQUEZ ZAVALA VILLANUEVA CAMACHO SALAS ARMENDARIZ GAMEZ VILLA
TAMEZ ALANIS ALANIZ GONZALES RODRIGUES MARIN CORTES CORTEZ MONTEMAYOR LIMON QUIROGA OLVERA HUERTA RENTERIA
""".split())
_GIVEN_NOT_SURNAME = {"SANTOS", "CRUZ", "REYES", "LUNA", "PAZ", "LUZ"}   # common given names too
_GIVEN_ALIASES = {"MA": "MARIA", "MARIE": "MARIA", "JOSE": "JOSE", "FCO": "FRANCISCO", "GPE": "GUADALUPE"}


def split_namelf(toks: list[str]) -> tuple[list[str], list[str]]:
    """NAMELF tokens -> (given names, surname tokens), keeping particle surnames
    ('DE LA CRUZ JUAN') and a second Hispanic surname ('GARCIA LOPEZ MARIA') together."""
    toks = [t.upper() for t in toks]
    if len(toks) < 2:
        return [], toks
    sur, i = [toks[0]], 1
    while sur[-1] in NAME_PARTICLES and i < len(toks) - 1:
        sur.append(toks[i])
        i += 1
    rest = toks[i:]

    def substantive(xs: list[str]) -> bool:   # the given name can't be just initials
        return any(len(x) >= 2 and x not in NAME_PARTICLES for x in xs)
    if len(rest) >= 2 and rest[0] in HISPANIC_SURNAMES and rest[0] not in _GIVEN_NOT_SURNAME and substantive(rest[1:]):
        sur.append(rest[0])
        rest = rest[1:]
    elif len(rest) >= 3 and rest[0] in NAME_PARTICLES and substantive(rest[2:]):   # 'GARCIA DE LEON MARIA'
        j = 0
        while j < len(rest) - 1 and rest[j] in NAME_PARTICLES:
            j += 1
        sur += rest[:j + 1]
        rest = rest[j + 1:]
    return rest, sur


def _surname_core(sur: list[str]) -> list[str]:
    """Surname tokens that identify a person ('DE LA CRUZ' -> ['CRUZ'], 'GARCIA LOPEZ' -> both)."""
    return [t for t in sur if t not in NAME_PARTICLES]


def extra_readings(raw: str) -> list[str]:
    """Readings the shared NAMELF parser misses: compound Hispanic / particle surnames, and
    Bell's 'LAST, FIRST & FIRST2 M LAST2' where the co-owner after '&' is written first-name-first."""
    out: list[str] = []
    raw = re.sub(r"\([^)]*\)", " ", _amp(raw))
    segs = [s.strip() for s in re.split(r"\s*&\s*", raw) if s.strip()]
    if not segs or is_entity(raw):
        return out
    comma_style = "," in segs[0]
    primary_sur: list[str] = []
    for i, seg in enumerate(segs):
        if ESTATE_RE.search(seg) and (_ESTATE_OF_SEG_RE.match(seg) or _THE_X_ESTATE_RE.match(seg)):
            continue
        toks = [t for t in to_namelf(seg).upper().split() if t not in _NAME_NOISE or t in NAME_PARTICLES]
        if len(toks) < 2:
            if i > 0 and toks and len(primary_sur) > 1:   # "& MARIA" shares a two-part surname
                out.append(" ".join(toks + primary_sur).title())
            continue
        if i > 0 and comma_style and "," not in seg:
            # prose order: FIRST [MIDDLE] LAST - and often she also uses the first owner's surname
            out.append(" ".join(toks).title())
            if primary_sur and toks[-1] not in primary_sur:
                out.append(" ".join(toks[:-1] + primary_sur).title())
            continue
        given, sur = split_namelf(toks)
        if i == 0:
            primary_sur = sur
        if not given or len(sur) < 2:
            continue   # a plain one-word surname: the shared parser already reads it right
        out.append(" ".join(given + sur).title())
        core = _surname_core(sur)
        if len(core) >= 2:   # 'Maria Garcia Lopez' is also recorded as 'Maria Garcia'
            out.append(" ".join(given + [core[0]]).title())
    return list(dict.fromkeys(out))


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[A-Z]{2,}", (s or "").upper()) if t not in _NAME_NOISE}


def is_entity(raw: str) -> bool:
    from obituary_enricher import _BUSINESS_RE  # noqa: PLC0415
    s = to_namelf(raw)
    return bool(s) and not s.startswith("*") and bool(_BUSINESS_RE.search(s) or _ENTITY_EXTRA_RE.search(s))


# ── stage 2: county research ─────────────────────────────────────────

def _http():
    import requests  # noqa: PLC0415
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def _pdf_text(blob: bytes) -> str:
    import pypdfium2 as pdfium  # noqa: PLC0415
    doc = pdfium.PdfDocument(blob)
    try:
        return "\n".join(p.get_textpage().get_text_range() for p in doc).replace("\r", "")
    finally:
        doc.close()


def _html_text(fragment: str) -> str:
    import html as _html  # noqa: PLC0415
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " | ", t)
    return re.sub(r"\s+", " ", _html.unescape(t))


def _mdy(s: str) -> str:
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            continue
        if d.year > date.today().year + 1:   # 2-digit-year rows: '10/29/73' -> 2073
            d = d.replace(year=d.year - 100)
        return d.strftime("%Y-%m-%d")
    return ""


def _money(s) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(s)))
    except ValueError:
        return None


class _Tcad:
    """TCAD Prodigy public API (token scoped to office=Travis, ~5 min life)."""
    BASE = "https://prod-container.trueprodigyapi.com"

    def __init__(self):
        self.s = _http()
        self.s.headers["Origin"] = "https://travis.prodigycad.com"
        self.t0 = 0.0

    def _auth(self):
        if time.time() - self.t0 > 200:
            for attempt in range(4):
                r = self.s.post(self.BASE + "/trueprodigy/cadpublic/auth/token", json={"office": "Travis"}, timeout=30)
                if r.ok and r.text.strip():
                    self.s.headers["Authorization"] = r.json()["user"]["token"]
                    self.t0 = time.time()
                    return
                time.sleep(3 * (attempt + 1))
            raise RuntimeError(f"TCAD token HTTP {r.status_code}")

    def _search(self, crit: dict) -> list[dict]:
        self._auth()
        body = {"pYear": {"operator": "=", "value": str(date.today().year)}, **crit}
        r = self.s.post(self.BASE + "/public/property/search?page=1&pageSize=20", json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"TCAD search HTTP {r.status_code}")
        if not r.text.strip():   # 200 + empty body = no match
            return []
        return (r.json() or {}).get("results") or []

    def property(self, apn: str) -> dict | None:
        """APN is either the TCAD PID (<=7 digits) or the 14-digit Tax Office account."""
        digits = re.sub(r"\D", "", apn)
        if len(digits) == 14:
            res = [x for x in self._search({"geoID": {"operator": "begins", "value": digits[:10]}})
                   if (x.get("refID2") or "") == digits]
        else:
            res = self._search({"pid": {"operator": "=", "value": str(int(digits))}})
        return res[0] if res else None

    def deeds(self, pid) -> list[dict]:
        self._auth()
        r = self.s.get(f"{self.BASE}/public/property/{int(pid)}/deeds", timeout=30)
        if not r.ok:
            raise RuntimeError(f"TCAD deeds HTTP {r.status_code}")
        if not r.text.strip():   # 200 + empty body = no deeds on file
            return []
        return (r.json() or {}).get("results") or []


def _travis_paid_by(text: str) -> tuple[str, str]:
    """Receipt PDF -> (process_type, paid_by). Paid By is blank on lockbox/transfer."""
    pt = re.search(r"Process Type:\s*(\S+)", text)
    m = re.search(r"Tender Type Tender Reference Paid By\n(.*?)\nEntity Net Taxable", text, re.S)
    payer = ""
    if m:
        lines = [l.strip() for l in m.group(1).split("\n") if l.strip()]
        last = lines[-1] if lines else ""
        mm = re.match(r"^[\d\-]+\s+(.*\D.*)$", last)
        if mm and not re.fullmatch(r"[\d\s\-]+", mm.group(1)):
            payer = mm.group(1).strip()
    return (pt.group(1) if pt else ""), payer


def research_travis(rec: dict, tcad: _Tcad) -> dict:
    prop = tcad.property(rec["apn"]) or {}
    if not prop:
        raise RuntimeError(f"TCAD has no 2026 property for APN {rec['apn']}")
    pid = str(prop["pid"])
    out = {"source": "TCAD Prodigy + go2gov", "pid": pid,
           "owner_raw": prop.get("displayName") or prop.get("name") or "",
           "mailing": " ".join(filter(None, [prop.get("addrDeliveryLine"), prop.get("addrCity"),
                                              prop.get("addrState"), prop.get("addrZip")])),
           "market_value": prop.get("marketValue"), "situs": prop.get("fullSitus") or "",
           "account": prop.get("refID2") or ""}
    out["deeds"] = [{"date": _mdy(d.get("deedDt") or ""), "type": d.get("deedDescription") or d.get("deedType") or "",
                     "grantor": d.get("seller") or "", "grantee": d.get("buyer") or "",
                     "instrument": d.get("instrumentNum") or ""} for d in tcad.deeds(pid)]
    acct = out["account"]
    out["payments"] = []
    if not acct:
        out["error"] = "no TCAD account"
        return out
    g = _http()
    g.post("https://travis.go2gov.net/cart/responsive/quickSearch.do",
           data={"criteria.heuristicSearch": acct}, timeout=30)
    info = _html_text(g.get(f"https://travis.go2gov.net/showPropertyInfo.do?account={acct}", timeout=30).text)
    # Each "... Taxes Due" table: Base | P&I | Fees | Total -> sum the Totals.
    due = 0.0
    for sec in info.split("Taxes Due")[1:]:
        amts = re.findall(r"\$([\d,]+\.\d\d)", sec.split("Taxes Due")[0])
        if len(amts) >= 4:
            due += _money(amts[3]) or 0
    out["total_due"] = round(due, 2)
    txt = _html_text(g.get(f"https://travis.go2gov.net/showPaymentReceipts.do?account={acct}", timeout=30).text)
    rows = re.findall(r"\b(\d{6,9})[\s|]+(\d{4})[\s|]+(\d\d/\d\d/\d{4})[\s|]+([\d,]+\.\d\d)", txt)
    pdf_dir = CACHE_DIR / "travis_receipts"
    for receipt, year, paid, amt in rows:
        cache = pdf_dir / f"{receipt}.json"
        meta = _load(cache)
        if meta is None:
            time.sleep(0.4)
            blob = g.get(f"https://travis.go2gov.net/exportReceipt?receipt={receipt}&account={acct}", timeout=60).content
            try:
                ptype, payer = _travis_paid_by(_pdf_text(blob))
            except Exception as e:  # noqa: BLE001
                ptype, payer = "", f"?pdf error {e.__class__.__name__}"
            meta = {"process_type": ptype, "payer": payer}
            _save(cache, meta)
        out["payments"].append({"date": _mdy(paid), "tax_year": int(year), "amount": _money(amt),
                                "receipt": receipt, "channel": meta["process_type"], "payer": meta["payer"]})
    return out


_WIL = "https://tax.wilcotx.gov"


def research_williamson(rec: dict) -> dict:
    qref = rec["apn"].strip().upper()
    g = _http()
    page = g.get(f"{_WIL}/Property-Detail?PropertyQuickRefID={qref}", timeout=60).text

    def by_id(suffix: str) -> str:
        m = re.search(rf'id="[^"]*{suffix}"[^>]*>(.*?)</td>', page, re.S)
        return _html_text(m.group(1)).replace("|", " ").strip() if m else ""

    owner = re.search(r'id="dnn_ctr385_View_divOwnersLabel"[^>]*>([^<]*)', page)
    out = {"source": "tax.wilcotx.gov", "account": qref,
           "owner_raw": " ".join((owner.group(1) if owner else "").split()),
           "mailing": " ".join(by_id("tdOIMailingAddress").split()),
           "exemptions": " ".join(by_id("tdOIExemptions").split()),
           "total_due": _money(by_id("tdPMTotalDue")) if by_id("tdPMTotalDue") else None}
    mv = re.search(r"Total Market Value[^$]*\$([\d,]+)", _html_text(page))
    out["market_value"] = _money(mv.group(1)) if mv else None
    # Deed table: date | seller | buyer | instr
    deeds = []
    i = page.find("DEED DATE")
    if i > 0:
        block = page[i:page.find("</table>", i)]
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
            cells = [_html_text(c).replace("|", " ").strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            cells = [" ".join(c.split()) for c in cells]
            if cells and re.match(r"\d{1,2}/\d{1,2}/\d{4}", cells[0]):
                # Positional: date | seller | buyer | instr # | vol/page (cells can be blank).
                cell = lambda i: cells[i] if len(cells) > i and cells[i] != "-" else ""  # noqa: E731
                grantor, grantee, instr = cell(1), cell(2), cell(3)
                if re.fullmatch(r"\d{6,}", grantee) and not instr:
                    grantee, instr = "", grantee
                deeds.append({"date": _mdy(cells[0]), "type": "", "grantor": grantor, "grantee": grantee,
                              "instrument": instr if re.fullmatch(r"\d{6,}", instr or "") else ""})
    out["deeds"] = deeds
    # Payments: every OpenReceiptPDF(...) call; one PDF per distinct payor id.
    calls = [re.findall(r"'([^']*)'", c) for c in re.findall(r"OpenReceiptPDF\(('[^)]*)\)", page)]
    payor_cache_p = CACHE_DIR / "williamson_payors.json"
    payors = _load(payor_cache_p, {}) or {}
    pays = []
    for c in calls:
        if len(c) != 7:
            continue
        q, txn, dt, amt, paid_for, payor, party = c
        key = payor or f"txn{txn}"
        if key not in payors:
            time.sleep(0.4)
            try:
                r = g.post(f"{_WIL}/Proxy-WilliamsonTax/tax/Receipt", timeout=60,
                           data=dict(QuickRefID=q, TransactionID=txn, PaidForPartyID=paid_for,
                                     PayorPartyID=payor, PartyID=party))
                fn = r.text.strip().strip('"')
                blob = g.get(f"{_WIL}/Proxy-WilliamsonTax/documents/pdf/{fn}/", timeout=60).content
                m = re.search(r"Payor:?[ \t]*(.*)", _pdf_text(blob))
                name = re.sub(r"\(\s*\)|\s+Owner:.*$", "", m.group(1)).strip() if m else ""
            except Exception as e:  # noqa: BLE001
                name = f"?receipt error {e.__class__.__name__}"
            payors[key] = name
            _save(payor_cache_p, payors)
        pays.append({"date": _mdy(dt), "tax_year": None, "amount": _money(amt), "receipt": txn,
                     "channel": "", "payer": payors[key], "payor_id": payor, "owner_party_id": party})
    out["payments"] = pays
    return out


def research_bell(rec: dict) -> dict:
    import bell_gsa  # noqa: PLC0415
    pid = rec["apn"]
    p = bell_gsa.fetch_parcel(pid)
    junk = re.compile(r",?\s*Request,?\s*Electronic,?\s*Communication", re.I)
    out = {"source": "belltx-search.gsacorp.io (no payer online)", "account": bell_gsa.pad_id(pid),
           "owner_raw": junk.sub("", p.get("owner_name") or "").strip(),
           "mailing": junk.sub("", f"{p.get('mailing_street', '')} {p.get('mailing_city_state_zip', '')}").strip(),
           "situs": p.get("situs") or ""}
    out["confidential"] = out["owner_raw"].startswith("*")
    vh = (p.get("value_history") or {}).get(str(date.today().year)) or {}
    out["market_value"] = _money(vh.get("Market")) if vh.get("Market") not in (None, "N/A") else None
    out["deeds"] = [{"date": d.get("date") or "", "type": d.get("type") or "",
                     "grantor": "" if d.get("grantor") == "*" else d.get("grantor") or "",
                     "grantee": "" if d.get("grantee") == "*" else d.get("grantee") or "",
                     "instrument": "" if "_instr" in (d.get("instrument") or "") else d.get("instrument") or ""}
                    for d in p.get("deeds") or []]
    t = bell_gsa.fetch_tax(pid)
    out["total_due"] = t.get("balance")
    out["tax_years"] = [{k: y.get(k) for k in ("year", "original", "paid", "balance", "delinquent")}
                        for y in t.get("years") or []]
    out["payments"] = []   # GSA shows paid-per-year only, never who paid
    return out


def stage_county(only: str | None = None, limit: int = 0, refresh: bool = False) -> None:
    cands = _load(CANDIDATES, {}) or {}
    tcad = _Tcad()
    done = 0
    for u, rec in cands.items():
        if rec.get("source") != "county_roll":   # a county-roll record knows its county ('Bell County Rural')
            rec["county"] = county_for_city(rec["city"], rec["zip5"], rec["apn"])
        if only and rec["county"].lower() != only.lower():
            continue
        if not gate1_passed(rec):
            continue   # Gate 1: no curative research on records without the equity
        cache = CACHE_DIR / "county" / f"{rec['county']}_{re.sub(r'[^A-Za-z0-9]', '', rec['apn'])}.json"
        if cache.exists() and not refresh:
            continue
        try:
            if rec["county"] == "Travis":
                res = research_travis(rec, tcad)
            elif rec["county"] == "Williamson":
                res = research_williamson(rec)
            else:
                res = research_bell(rec)
            res["fetched"] = datetime.now().isoformat(timespec="seconds")
            _save(cache, res)
            logger.info("%s %s %s: owner=%r deeds=%d payments=%d", rec["county"], rec["apn"], rec["street"],
                        res.get("owner_raw"), len(res.get("deeds") or []), len(res.get("payments") or []))
        except Exception as e:  # noqa: BLE001
            logger.warning("%s %s %s: county research failed: %s", rec["county"], rec["apn"], rec["street"], e)
        done += 1
        if limit and done >= limit:
            break
    _save(CANDIDATES, cands)   # persists any county correction above


def county_data(rec: dict) -> dict:
    cd = _load(CACHE_DIR / "county" / f"{rec['county']}_{re.sub(r'[^A-Za-z0-9]', '', rec.get('apn') or '')}.json", {}) or {}
    limit = str(date.today().year + 1)
    for d in cd.get("deeds") or []:
        if (d.get("date") or "")[:4] > limit:
            d["date"] = f"{int(d['date'][:4]) - 100}{d['date'][4:]}"
    return cd


# ── stage 3: probate docket (Williamson PublicAccess, name search) ───

_CASE_RE = re.compile(r"^\d{2}-\d{4}-CP\d", re.I)


def survivor_holds_title(deeds: list[dict], owner_raw: str) -> dict | None:
    """The newest DEATH deed already settled the death: a death certificate / heirship instrument that
    leaves every current owner on title and drops someone ('LYNN ALVIN & MARY' -> 'LYNN MARY'), or a
    court-sanctioned conveyance (probated will, executor's / administrator's deed, muniment) to the
    current owner. Later ordinary deeds don't undo it: Boriskie took Robert's share by will in 1996
    and signed a deed in 2013, so Robert's death is not this owner's problem."""
    newest = [d for d in sorted(deeds, key=lambda d: d.get("date") or "", reverse=True)
              if DEATH_DEED_RE.search(d.get("type") or "")]
    if not newest or not owner_raw or ESTATE_RE.search(owner_raw):
        return None
    d = newest[0]
    if " ".join((d.get("grantor") or "").split()).upper() == " ".join((d.get("grantee") or "").split()).upper():
        return None   # 'CALDWELL DEMPSEY &' -> 'CALDWELL DEMPSEY &': truncated both sides, says nothing
    owners, before = person_names(owner_raw), person_names(d.get("grantor") or "")
    after = person_names(d.get("grantee") or "")
    if not (owners and after and all(any(_same_person(o, a) for a in after) for o in owners)):
        return None
    if re.search(r"EXECUTOR|ADMINISTRAT|WILL\b|PROBATE|MUNIMENT", d.get("type") or "", re.I) \
            or len(before) > len(after):
        return d
    return None


def recent_grantees(deeds: list[dict], owner_raw: str = "") -> list[str]:
    """Grantee text of the two newest deeds, minus anyone who is the GRANTOR of a newer deed:
    they sold out and are off title (Elgin's trustee sold 211 Palmetto to Trafton in 2024, so
    Elgin's 2016 death is not this owner's title problem).

    With the owner of record, grantees of deeds older than the deed that vested that owner from
    someone else are off title too: Brenda Johnson took 1924 Valley Hill from Faires in 1992, so
    the Dlugos couple on a 1981 deed (another branch of the CAD list) are not her problem."""
    newest = sorted(deeds, key=lambda d: d.get("date") or "", reverse=True)
    vest = ""
    owners = person_names(owner_raw) if owner_raw else []
    for d in newest:
        g = d.get("grantee") or ""
        if owners and any(_same_person(o, g) for o in owners) and \
                not any(_same_person(o, d.get("grantor") or "") for o in owners):
            vest = d.get("date") or ""
            break
    out = []
    for j, d in enumerate(newest[:2]):
        g = d.get("grantee") or ""
        if any(_same_person(n, newer.get("grantor") or "") for newer in newest[:j] for n in person_names(g)):
            continue
        if vest and (d.get("date") or "") < vest and not any(_same_person(o, g) for o in owners):
            continue
        out.append(g)
    return out


def _decedent_names(rec: dict, cd: dict) -> list[str]:
    """Who might be dead: the county owner of record + the last grantees."""
    names: list[str] = []
    for raw in [cd.get("owner_raw", "")] + recent_grantees(cd.get("deeds") or [], cd.get("owner_raw", "")):
        for n in person_names(raw):
            if n not in names:
                names.append(n)
    # The CRM name is only a fallback: when it differs from the county owner
    # the CRM is stale, and searching it finds strangers' estates.
    if not names and rec.get("owner_first") and rec.get("owner_last"):
        names.append(f"{rec['owner_first']} {rec['owner_last']}".title())
    return names[:4]


async def _probate_search(names: dict[str, list[str]]) -> dict:
    from playwright.async_api import async_playwright  # noqa: PLC0415
    from scrapers.probate_odyssey import (  # noqa: PLC0415
        WILCO_BASE, _establish_session, _fetch_case_detail, _navigate_to_search)
    base = root = WILCO_BASE
    results: dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await _establish_session(page, root)
        for full in sorted({n for ns in names.values() for n in ns}):
            parts = full.split()
            first, last = parts[0], parts[-1]
            if last.lower() in ("jr", "sr", "ii", "iii", "iv") and len(parts) > 2:
                last = parts[-2]
            hits, other = [], []
            try:
                await _navigate_to_search(page, root, 200)
                await page.check("#Party")
                await page.fill("#LastName", last)
                await page.fill("#FirstName", first)
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
                    await page.click("#SearchSubmit")
                await page.wait_for_timeout(1200)
                rows = await page.evaluate("""() => [...document.querySelectorAll('tr')].map(r => {
                    const a = r.querySelector('a[href*=CaseDetail]');
                    return {cells: [...r.cells].map(c => c.innerText.trim()), href: a ? a.getAttribute('href') : ''};
                }).filter(r => r.href)""")
                ftok, ltok = first.upper(), last.upper()
                for r in rows:
                    cells = r["cells"]
                    case_no = cells[0] if cells else ""
                    style = cells[1] if len(cells) > 1 else ""
                    su = style.upper()
                    if not (_CASE_RE.match(case_no) or "ESTATE OF" in su):
                        typ = " / ".join(c.replace("\n", " ") for c in cells[3:5])
                        if _OTHER_CASE_RE.search(typ) and ltok in su and ftok in su:
                            other.append({"case_number": case_no, "style": style, "type_status": typ,
                                          "filed": _mdy((cells[2] if len(cells) > 2 else "").split("\n")[0])})
                        continue
                    if "ESTATE OF" not in su or ltok not in su or ftok not in su:
                        continue
                    hits.append({"case_number": case_no, "style": style,
                                 "filed": _mdy((cells[2] if len(cells) > 2 else "").split("\n")[0]),
                                 "type_status": " / ".join(c.replace("\n", " ") for c in cells[3:5]),
                                 "href": r["href"]})
                for h in hits[:2]:
                    try:
                        d = await _fetch_case_detail(page, base, h["href"])
                        h.update({k: d.get(k) for k in ("pr_name", "pr_role", "attorney_name", "attorney_phone",
                                                         "date_of_death", "letters_date", "inventory_date",
                                                         "last_event_date")})
                        await page.go_back()
                    except Exception as e:  # noqa: BLE001
                        h["detail_error"] = str(e)[:120]
            except Exception as e:  # noqa: BLE001
                logger.warning("probate search %s failed: %s", full, e)
                results[full] = {"error": str(e)[:200]}
                try:   # a dropped connection kills the session; every later search would fail too
                    await _establish_session(page, root)
                except Exception as e2:  # noqa: BLE001
                    logger.warning("probate: session re-establish failed: %s", e2)
                continue
            results[full] = {"cases": hits, "other": other}
            logger.info("docket %-28s -> %d estate case(s), %d other", full, len(hits), len(other))
        await browser.close()
    return results


def stage_probate(refresh: bool = False) -> None:
    import asyncio  # noqa: PLC0415
    cands = _load(CANDIDATES, {}) or {}
    cache_p = CACHE_DIR / "probate_williamson.json"
    cache = {} if refresh else (_load(cache_p, {}) or {})
    todo: dict[str, list[str]] = {}
    for u, rec in cands.items():
        if rec["county"] != "Williamson" or not gate1_passed(rec):
            continue
        # entries from before the docket kept tax/divorce/title cases are re-searched
        names = [n for n in _decedent_names(rec, county_data(rec)) if "other" not in (cache.get(n) or {})]
        if names:
            todo[u] = names
    if not todo:
        logger.info("probate: nothing new to search")
        return
    cache.update(asyncio.run(_probate_search(todo)))
    _save(cache_p, cache)


def stage_docket(only: str | None = None, workers: int = 3) -> None:
    """Travis + Bell court dockets (Odyssey Portal; Travis solves a reCAPTCHA per search via 2Captcha).

    Gate 1 passes only, records with a death sign first, up to 3 names per record."""
    import odyssey_portal as op  # noqa: PLC0415
    cands = _load(CANDIDATES, {}) or {}
    for county in ("Bell", "Travis"):
        if only and county.lower() != only.lower():
            continue
        cache_p = CACHE_DIR / f"probate_{county.lower()}.json"
        cache = _load(cache_p, {}) or {}
        first, rest = [], []
        for rec in cands.values():
            if rec["county"] != county or not gate1_passed(rec):
                continue
            cd = county_data(rec)
            names = [n for n in _decedent_names(rec, cd)[:3] if n not in cache]
            hot = rec.get("signals") or rec.get("owner_deceased") or ESTATE_RE.search(cd.get("owner_raw") or "")
            (first if hot else rest).extend(names)
        todo = list(dict.fromkeys(first + rest))
        logger.info("%s docket: %d names to search (%d from records with a death sign)", county, len(todo),
                    len(set(first)))
        op.search_names_parallel(county, todo, cache, save=lambda c, p=cache_p: _save(p, c), workers=workers)


_DOCKETS: dict = {}


def _docket_cache(county: str) -> dict:
    """The county's docket cache, re-read only when the file changes (scoring reads it per record)."""
    p = CACHE_DIR / f"probate_{county.lower()}.json"
    mt = p.stat().st_mtime if p.exists() else 0
    if _DOCKETS.get(county, (None,))[0] != mt:
        _DOCKETS[county] = (mt, _load(p, {}) or {})
    return _DOCKETS[county][1]


def probate_cases(rec: dict, cd: dict) -> list[dict]:
    cache = _docket_cache(rec["county"])
    out, seen = [], set()
    for n in _decedent_names(rec, cd):
        for h in (cache.get(n) or {}).get("cases") or []:
            if h["case_number"] not in seen:
                seen.add(h["case_number"])
                out.append(h)
    return out


def court_cases(rec: dict, cd: dict) -> list[dict]:
    """Tax suits / divorces / heirship / title cases naming someone on title (all three dockets)."""
    cache = _docket_cache(rec["county"])
    out, seen = [], set()
    for n in _decedent_names(rec, cd):
        for h in (cache.get(n) or {}).get("other") or []:
            if h["case_number"] not in seen:
                seen.add(h["case_number"])
                out.append(h)
    return out


# ── stage 4: obituary (the ONE obituary engine, unmodified) ──────────

def _roll_death_sign(rec: dict, cd: dict) -> bool:
    """A county-roll record has no DataSift deceased lists, so the free research stands in:
    an estate case on the docket, a death deed in the chain, or a Find A Grave memorial."""
    if probate_cases(rec, cd):
        return True
    deeds = sorted(cd.get("deeds") or [], key=lambda d: d.get("date") or "", reverse=True)
    if any(DEATH_DEED_RE.search(d.get("type") or "") for d in deeds):
        return True
    global _VERIFY_CACHE
    if _VERIFY_CACHE is None:
        _VERIFY_CACHE = _load(CACHE_DIR / "verify.json", {}) or {}
    return bool(memorial_deaths(rec, cd, deeds, _VERIFY_CACHE)[0])


_VERIFY_CACHE = None


def stage_obit(limit: int = 0, refresh: bool = False, workers: int = 4) -> None:
    import config  # noqa: PLC0415
    from notice_parser import NoticeData  # noqa: PLC0415
    from obituary_enricher import enrich_obituary_data  # noqa: PLC0415

    cands = _load(CANDIDATES, {}) or {}
    cache_p = CACHE_DIR / "obituary.json"
    cache = {} if refresh else (_load(cache_p, {}) or {})
    notices, keys = [], []
    for u, rec in cands.items():
        cd = county_data(rec)
        owner_raw = cd.get("owner_raw") or rec.get("owner_company") or \
            f"{rec.get('owner_last', '')} {rec.get('owner_first', '')}"
        nl = to_namelf(owner_raw)
        if u in cache or not person_names(owner_raw) or not gate1_passed(rec):
            continue
        # paid searches (Serper + Claude): only where something already says the owner may be dead
        if not rec.get("signals") and not ESTATE_RE.search(owner_raw) and not rec.get("owner_deceased")                 and not (rec.get("source") == "county_roll" and _roll_death_sign(rec, cd)):
            continue
        n = NoticeData(address=rec["street"], city=rec["city"], state="TX", zip=rec["zip5"],
                       county=rec["county"], notice_type="tax_delinquent", tax_owner_name=nl)
        notices.append(n)
        keys.append(u)
        if limit and len(notices) >= limit:
            break
    if not notices:
        logger.info("obit: nothing new to search")
        return
    logger.info("obit: searching %d owners", len(notices))
    enrich_obituary_data(notices, config.ANTHROPIC_API_KEY, skip_dm_address=True, skip_ancestry=True,
                         max_heir_depth=1, workers=workers)
    fields = ("owner_deceased", "date_of_death", "obituary_url", "obituary_source_type", "decedent_name",
              "decision_maker_name", "decision_maker_relationship", "decision_maker_status",
              "decision_maker_2_name", "decision_maker_2_relationship",
              "decision_maker_3_name", "decision_maker_3_relationship", "heir_map_json", "dm_confidence")
    for u, n in zip(keys, notices):
        cache[u] = {f: getattr(n, f, "") for f in fields}
        cache[u]["searched_name"] = n.tax_owner_name
    _save(cache_p, cache)
    logger.info("obit: %d confirmed deceased of %d searched",
                sum(1 for k in keys if cache[k].get("owner_deceased") == "yes"), len(keys))


# ── stage 4b: verify (Find A Grave + birth years, free) ──────────────
#
# Name searches hit strangers. A birth year is the cheapest tie-breaker:
# DataSift's owner age keeps counting after death (age now = today - DOB), so
# today.year - age is the owner's birth year, and an obituary / memorial whose
# birth year is off by more than a few years is a different person.

_FAG = "https://www.findagrave.com"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}


def _fag_date(s: str) -> tuple[int | None, str]:
    """'8 Oct 1974' -> (1974, '1974-10-08'); 'Oct 1974' / '1974' -> (1974, '1974-12-31' - dead BY year end)."""
    s = (s or "").strip()
    m = re.match(r"(?:(\d{1,2})\s+)?([A-Za-z]{3})[a-z]*\.?\s+(\d{4})$", s)
    if m and m.group(2).lower() in _MONTHS:
        y, mo = int(m.group(3)), _MONTHS[m.group(2).lower()]
        return y, (f"{y}-{mo:02d}-{int(m.group(1)):02d}" if m.group(1) else f"{y}-{mo:02d}-28")
    m = re.search(r"(\d{4})", s)
    return (int(m.group(1)), f"{m.group(1)}-12-31") if m else (None, "")


def parse_findagrave(html_text: str) -> list[dict]:
    """Search-results page -> [{id, name, birth_year, death_year, death_date, place}]."""
    import html as _h  # noqa: PLC0415
    out = []
    for blk in re.split(r'(?=<div class="memorial-item[ "])', html_text)[1:]:
        mid = re.search(r'href="/memorial/(\d+)/', blk)
        nm = re.search(r'class="name-grave[^"]*"[^>]*>\s*<i[^>]*>(.*?)</i>', blk, re.S)
        dates = re.search(r'class="birthDeathDates[^"]*"[^>]*>(.*?)</b>', blk, re.S)
        place = re.search(r'class="addr-cemet[^"]*"[^>]*>(.*?)</p>', blk, re.S)
        if not (mid and nm):
            continue
        b, d = "", ""
        if dates:
            parts = [x.strip() for x in re.split(r"–|&ndash;|-", _h.unescape(_html_text(dates.group(1))))]
            b, d = (parts + ["", ""])[:2]
        by, _ = _fag_date(b)
        dy, dd = _fag_date(d)
        out.append({"id": mid.group(1), "name": " ".join(_h.unescape(_html_text(nm.group(1))).replace("|", " ").split()),
                    "birth_year": by, "death_year": dy, "death_date": dd,
                    "place": " ".join(_h.unescape(_html_text(place.group(1))).replace("|", " ").split())
                    if place else ""})
    return out


def findagrave_search(first: str, last: str, s=None) -> list[dict]:
    """Texas memorials for first+last (Find A Grave's location filter needs an
    internal id, so filter on the cemetery address instead)."""
    s = s or _http()
    for wait in (0, 60, 180):   # 429s come in bursts after ~50 quick searches
        time.sleep(wait)
        r = s.get(f"{_FAG}/memorial/search", params={"firstname": first, "lastname": last}, timeout=30)
        if r.status_code != 429:
            break
    if r.status_code != 200:
        raise RuntimeError(f"Find A Grave HTTP {r.status_code}")
    return [m for m in parse_findagrave(r.text) if re.search(r"\bTexas\b", m["place"])]


_BORN_RE = re.compile(r"\bborn\b[^.]{0,80}?\b(1[89]\d\d|20[0-2]\d)\b", re.I)
_SPAN_RE = re.compile(r"\b(1[89]\d\d)\s*(?:-|–|—|to)\s*(19\d\d|20[0-2]\d)\b")
_AGE_RE = re.compile(r"\b(?:at the age of|aged?|age)\s+(\d{2,3})\b|,\s*(\d{2,3}),\s+(?:of|passed|died|went)\b", re.I)


def obituary_birth_year(text: str, dod: str = "") -> int | None:
    """Birth year stated or implied by an obituary page (born YYYY / YYYY-YYYY / age N at a known DOD)."""
    text = text or ""
    m = _BORN_RE.search(text)
    if m:
        return int(m.group(1))
    m = _SPAN_RE.search(text)
    if m and (not dod or m.group(2) == dod[:4]):
        return int(m.group(1))
    m = _AGE_RE.search(text)
    if m and dod[:4].isdigit():
        age = int(m.group(1) or m.group(2))
        if 18 <= age <= 110:
            return int(dod[:4]) - age
    return None


def owner_birth_year(rec: dict) -> int | None:
    try:
        age = int(rec.get("owner_age") or 0)
    except (TypeError, ValueError):
        return None
    return date.today().year - age if 18 <= age <= 115 else None


def stage_verify(refresh: bool = False) -> None:
    """Find A Grave per person on title + the birth year in each matched obituary."""
    cands = _load(CANDIDATES, {}) or {}
    obits = _load(CACHE_DIR / "obituary.json", {}) or {}
    cache_p = CACHE_DIR / "verify.json"
    cache = {} if refresh else (_load(cache_p, {}) or {})
    fag, obit_by = cache.setdefault("findagrave", {}), cache.setdefault("obit_birth", {})
    s = _http()
    n = 0
    for u, rec in cands.items():
        cd = county_data(rec)
        if not cd or is_entity(cd.get("owner_raw") or "") or not gate1_passed(rec):
            continue
        deeds = sorted(cd.get("deeds") or [], key=lambda d: d.get("date") or "", reverse=True)
        for name in county_people(rec, cd, deeds)[:4]:
            p = _name_parts(name)
            key = name.lower()
            if not p or key in fag:
                continue
            time.sleep(3.0)
            try:
                fag[key] = findagrave_search(p[0].title(), p[2].title(), s)
            except Exception as e:  # noqa: BLE001
                logger.warning("findagrave %s: %s", name, e)
                continue
            n += 1
            if n % 10 == 0:
                _save(cache_p, cache)
        ob = obits.get(u) or {}
        url = ob.get("obituary_url") or ""
        if ob.get("owner_deceased") == "yes" and url and u not in obit_by:
            try:
                r = s.get(url, timeout=30)
                txt = _html_text(r.text) if r.ok else ""
            except Exception:  # noqa: BLE001
                txt = ""
            obit_by[u] = {"url": url, "birth_year": obituary_birth_year(txt, ob.get("date_of_death") or ""),
                          "fetched": bool(txt)}
    _save(cache_p, cache)
    logger.info("verify: %d Find A Grave searches; %d obituaries checked for a birth year", n, len(obit_by))


def _signed_after(county: str, name: str, after: str) -> dict | None:
    import clerk_records as cr  # noqa: PLC0415
    return cr.signed_after(county, name, after)


_HOME_AREA_RE = re.compile(r"\b(Travis|Williamson|Bell|Hays|Bastrop|Burnet|Milam|Coryell|Lampasas|McLennan|Caldwell|"
                           r"Lee|Blanco|Falls) County\b", re.I)


def _full_middle_agrees(memorial: str, name: str) -> bool:
    """'Virginia Ann' on both sides, not just the initial: 'Jorge Luis' vs 'Jorge L' could be anyone."""
    a, b = _name_parts(memorial), _name_parts(name)
    return bool(a and b and a[1] and b[1] and len(b[1][0]) > 1 and a[1][0] == b[1][0])


def memorial_deaths(rec: dict, cd: dict, deeds: list[dict], verify: dict) -> tuple[list[dict], list[str]]:
    """Find A Grave memorials accepted as a death of someone on title, + 'possible' lines.

    Accepted: a Texas memorial whose name agrees on a middle initial / suffix
    (strong), or a first+last match whose birth year is within 2 of DataSift's
    owner age. Never: a memorial dated before that person took title, or one
    contradicted by a later deed TO that person."""
    accepted, possible = [], []
    born = owner_birth_year(rec)
    crm = f"{rec.get('owner_first') or ''} {rec.get('owner_last') or ''}".strip()
    seen_ids: set = set()
    people = county_people(rec, cd, deeds)
    for name in people[:4]:
        mems = (verify.get("findagrave") or {}).get(name.lower()) or []
        # the other readings of this same person ('Rogelio Cruz Aguilar' for 'Rogelio Aguilar'):
        # a memorial that conflicts with any of them is a namesake
        fuller = [n for n in people if n != name and name_match(n, name) != "no"]
        acquired = min((d.get("date") or "" for d in deeds if _same_person(name, d.get("grantee") or "")),
                       default="")
        for m in mems:
            g = name_match(m["name"], name)
            if g == "no" or not m.get("death_date") or m["id"] in seen_ids:
                continue
            if any(name_match(m["name"], n2) == "no" for n2 in fuller):
                continue
            if acquired and m["death_date"] < acquired:
                continue   # died before buying: a different person
            by, dy = m.get("birth_year"), m.get("death_year")
            if by and dy and dy - by < 18:
                continue   # died as a child: not an owner
            if dy and born and dy - born < 18 and (name_match(name, crm) != "no" if crm else False):
                continue
            if took_title_after(name, deeds, m["death_date"]):
                continue
            if _signed_after(rec.get("county") or "", name, m["death_date"]):
                continue   # signed a recorded instrument after that death: alive, a namesake
            hint = born if crm and (name_match(name, crm) != "no" or _same_person(crm, name)) else None
            by = m.get("birth_year")
            if hint and by and abs(by - hint) > 3:
                continue
            # far from home needs two things agreeing: a burial elsewhere is normal, a namesake elsewhere more so
            local = bool(_HOME_AREA_RE.search(m.get("place") or "")) or _full_middle_agrees(m["name"], name) \
                or (g == "strong" and hint and by and abs(by - hint) <= 2)
            if local and (g == "strong" or (hint and by and abs(by - hint) <= 2)):
                accepted.append({**m, "name": " ".join(m["name"].replace("|", " ").replace("�", '"').split()), "person": name, "why": "middle name/initial agrees" if g == "strong"
                                 else f"birth year {by} matches owner age (~{hint})"})
                seen_ids.add(m["id"])
                break
            possible.append(f"Find A Grave #{m['id']} {m['name']} ({m.get('birth_year') or '?'}-"
                            f"{m.get('death_year') or '?'}, {m['place']}) - first+last only, verify it is {name}")
    return accepted, possible[:3]


# ── stage 4c: Ancestry (death indexes + 1950 census, logged-in browser) ──
#
# Budgeted by ancestry_enricher's daily page-load cap (100): each person costs
# 1-2 loads, so a run does the most useful ~40 people and the next run resumes.
# Records whose death is already court/recorded proof, Drops and entities are
# skipped; records with no death proof go first.

_INDEX_RE = re.compile(r"Social Security Death|Death Index|Death Certificate|Death Record|Burial", re.I)


def stage_ancestry(limit: int = 0, refresh: bool = False) -> None:
    import asyncio  # noqa: PLC0415

    import ancestry_enricher as anc  # noqa: PLC0415
    cands = _load(CANDIDATES, {}) or {}
    obits = _load(CACHE_DIR / "obituary.json", {}) or {}
    verify = _load(CACHE_DIR / "verify.json", {}) or {}
    cache_p = CACHE_DIR / "ancestry.json"
    cache = {} if refresh else (_load(cache_p, {}) or {})
    rank = {"none": 0, "memorial only": 1, "obituary only": 1, "obituary + memorial": 2,
            "county roll estate label": 3}
    todo = []
    for u, rec in cands.items():
        cd = county_data(rec)
        if not cd or is_entity(cd.get("owner_raw") or ""):
            continue
        a = assess(rec, cd, obits.get(u) or {}, probate_cases(rec, cd), verify, court_cases(rec, cd))
        # Gate 3 only: equity after all known arrears + a curative problem
        if not a["gate"].startswith("3") or a["death_proof"] == "court/recorded":
            continue
        deeds = a["deeds"]
        crm = f"{rec.get('owner_first') or ''} {rec.get('owner_last') or ''}".strip()
        seen_people: dict = {}
        for name in county_people(rec, cd, deeds)[:4]:
            p = _name_parts(name)
            if not p or is_entity(name) or re.search(r"\b(COUNTY|TRUST|LIVING|CITY|STATE)\b", name, re.I):
                continue   # 'County Dovemeadow Travis', 'Carol Domaschk Living Trust Green'
            key = (p[0], p[2])
            if key in seen_people:   # 'Ruth Hall' after 'Ruth Ann Hall': one search per person
                continue
            seen_people[key] = name
            if len(seen_people) > 2 or any(k.startswith(f"{p[0].lower()} ") and k.endswith(f" {p[2].lower()}")
                                           for k in cache):
                continue
            born = owner_birth_year(rec) if crm and (name_match(name, crm) != "no" or _same_person(crm, name)) \
                else None
            took = min((d.get("date") or "" for d in deeds if _same_person(name, d.get("grantee") or "")),
                       default="")
            todo.append((rank.get(a["death_proof"], 3), -(a.get("spread") or 0), name, born, took))
    todo.sort()
    if limit:
        todo = todo[:limit]
    if not todo:
        logger.info("ancestry: nothing new to search")
        return
    logger.info("ancestry: %d people queued (%d page loads left today)", len(todo),
                anc.DAILY_LIMIT - anc._get_page_loads_today())

    async def run():
        pw, ctx, page = await anc.launch_browser()
        if not page:
            logger.error("ancestry: login failed - run: python src/ancestry_enricher.py bootstrap")
            return
        try:
            for _, _, name, born, took in todo:
                if anc.is_circuit_broken() or anc.DAILY_LIMIT - anc._get_page_loads_today() < 3:
                    logger.warning("ancestry: stopping (daily page budget / circuit breaker); re-run tomorrow")
                    break
                try:
                    cache[name.lower()] = await anc.research_person(page, name, born, took)
                except Exception as e:  # noqa: BLE001
                    logger.warning("ancestry %s: %s", name, e)
                    continue
                d = cache[name.lower()].get("death")
                logger.info("ancestry %-30s -> %s", name, f"{d['collection']} d.{d['death']}" if d else "no death")
                _save(cache_p, cache)
        finally:
            await anc.close_browser(pw, ctx)
    asyncio.run(run())
    _save(cache_p, cache)


def ancestry_deaths(rec: dict, cd: dict, deeds: list[dict], verify: dict) -> tuple[list[dict], list[str]]:
    """Accepted Ancestry death records (same shape as memorials) + 1950-census leads for the Notes."""
    import ancestry_enricher as anc  # noqa: PLC0415
    accepted, leads = [], []
    cache = verify.get("ancestry") or {}
    done = set()
    for name in county_people(rec, cd, deeds)[:4]:
        p = _name_parts(name)
        if not p or (p[0], p[2]) in done:
            continue
        # the stage searches one spelling per person ('Ruth Ann Hall', not also 'Ruth Hall')
        res = cache.get(name.lower()) or next(
            (v for k, v in cache.items() if (_name_parts(k) or ("",))[0] == p[0] and (_name_parts(k) or ("", "", ""))[2] == p[2]),
            None)
        if not res:
            continue
        done.add((p[0], p[2]))
        d = res.get("death")
        if d:   # re-check cached picks against the current rules (place, title date)
            first, middle, last = anc._parse_owner_name(name)
            took = min((x.get("date") or "" for x in deeds if _same_person(name, x.get("grantee") or "")),
                       default="")
            d = anc.pick_death_record([d], first, last, middle, res.get("birth_year_hint"), took)
        if d and d.get("death_year"):
            _, dd = _fag_date(re.sub(r"\bx+\b", "", d.get("death") or "").strip() or str(d["death_year"]))
            if not took_title_after(name, deeds, dd):
                accepted.append({"person": name, "death_date": dd, "death_year": d["death_year"],
                                 "birth_year": d.get("birth_year"), "name": d.get("name"),
                                 "source": f"Ancestry: {d.get('collection')}", "url": d.get("url"),
                                 "index": bool(_INDEX_RE.search(d.get("collection") or ""))})
        for c in res.get("census") or []:
            leads.append(f"1950 census lead for {name} (verify it is the same person): {c['name']}, b. "
                         f"{c.get('birth_year') or '?'} {c.get('birthplace') or ''}, {c.get('relation')} in "
                         f"{c.get('home_1950')}; household: {', '.join(c.get('relatives') or []) or '-'}")
    return accepted, leads


# ── delinquency years + spread (Jordan John's triage) ────────────────
#
# 1 year behind = low motivation; 2-4 years = the title-problem sweet spot;
# 5+ years = often heir-occupied (check who lives there before paying for research).
# Spread = CAD market value x 60% (conservative as-is, no comps) minus the taxes owed;
# under $100K the research is not worth the squeeze.

AS_IS_FACTOR = 0.60
SPREAD_MIN = 100_000
SPREAD_FLOOR = 25_000


def _latest_delq_year() -> int:
    from scrapers.tax_delinquent_wilco import latest_delinquent_tax_year  # noqa: PLC0415
    return latest_delinquent_tax_year()


def _indexed(src: Path, cache: Path, build) -> dict:
    """Small {key: value} index of a big county roll, rebuilt when the roll changes."""
    if not src.exists():
        return {}
    idx = _load(cache, {}) or {}
    if idx.get("_src_mtime") == src.stat().st_mtime:
        return idx
    idx = build(src)
    idx["_src_mtime"] = src.stat().st_mtime
    _save(cache, idx)
    return idx


def _build_travis_since(src: Path) -> dict:
    import csv  # noqa: PLC0415

    import travis_tax_schema as schema  # noqa: PLC0415
    out: dict = {}
    with open(src, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(schema.sanitize_csv_lines(f, source=src.name))
        for raw in reader:
            row = schema.normalize_delq_row(raw)
            acct = re.sub(r"\D", "", row.get("Account #") or "")
            since = (row.get("1st Year Delinquent") or "").strip()
            if not acct or not since.isdigit():
                continue
            y = int(since)
            cur = out.get(acct)
            # '0000' = delinquent on the current roll only; a real year beats it
            if cur is None or (y and (cur == 0 or y < cur)):
                out[acct] = y
    return out


def _build_wilco_years(src: Path) -> dict:
    import openpyxl  # noqa: PLC0415
    out: dict = {}
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    ws = next(s for s in wb.worksheets if s.max_row and s.max_row > 1)
    rows = ws.iter_rows(values_only=True)
    col = {h: i for i, h in enumerate(next(rows)) if h is not None}
    qi, yi = col["Quick Ref"], col["Tax Year"]
    for row in rows:
        qr = str(row[qi] or "").strip().upper()
        try:
            y = int(row[yi])
        except (TypeError, ValueError):
            continue
        if qr.startswith("R"):
            out.setdefault(qr, set()).add(y)
    wb.close()
    return {k: sorted(v) for k, v in out.items()}


def delinquency(rec: dict, cd: dict) -> dict:
    """{'years': N tax years delinquent (latest delinquent year - first unpaid + 1), 'first_year', 'source'}.

    Same yardstick in all three counties; a current-year bill is never delinquent."""
    latest = _latest_delq_year()
    county = rec.get("county")
    first = None
    src = ""
    if county == "Travis":
        idx = _indexed(ROOT / "data" / "travis_tax_cache" / "TaxDelqOpenData.csv",
                       CACHE_DIR / "travis_delq_since.json", _build_travis_since)
        since = idx.get(re.sub(r"\D", "", cd.get("account") or ""))
        if since is not None:
            first, src = (since or latest), "Travis delinquent roll (SINCE)"
        elif idx:
            src = "not on the Travis delinquent roll"
    elif county == "Williamson":
        idx = _indexed(ROOT / "data" / "wilco_tax_cache" / "wilco_delinquent_latest.xlsx",
                       CACHE_DIR / "wilco_delq_years.json", _build_wilco_years)
        years = [y for y in idx.get((cd.get("account") or rec.get("apn") or "").strip().upper()) or [] if y <= latest]
        if years:
            first, src = min(years), "Williamson delinquent roll"
        elif idx:
            src = "not on the Williamson delinquent roll"
    elif county == "Bell":
        years = [y["year"] for y in cd.get("tax_years") or []
                 if y.get("year") and y["year"] <= latest and (y.get("delinquent") or (y.get("balance") or 0) > 0)]
        if years:
            first, src = min(years), "Bell GSA per-year balances"
        elif cd.get("tax_years"):
            src = "no delinquent year on Bell GSA"
    return {"years": (latest - first + 1) if first else 0, "first_year": first, "source": src}


# ── GATE 1: equity = 60% of the CAD value minus ALL known arrears ────
#
# Arrears = anything owed against the property, not just taxes: back taxes
# (county), the mortgage balance DataSift estimates from its equity %, and
# (Gate 2) every open lien / judgment / deed of trust the clerk records show.
# Amounts the public index does not carry are listed as UNKNOWN warnings,
# never guessed. Only Gate 1 passes get the curative research.

GATE_MIN = 75_000
VALUE_DIR = CACHE_DIR / "value"
DISTRESS_LISTS = re.compile(r"FORECLOS|PENDENS|LIEN|BANKRUPT|PRE-?FORECLOS|NOTICE OF (SALE|DEFAULT)|AUCTION", re.I)


def _build_travis_due(src: Path) -> dict:
    import csv  # noqa: PLC0415

    import travis_tax_schema as schema  # noqa: PLC0415
    out: dict = {}
    with open(src, encoding="utf-8-sig", errors="replace", newline="") as f:
        for raw in csv.DictReader(schema.sanitize_csv_lines(f, source=src.name)):
            row = schema.normalize_delq_row(raw)
            acct = re.sub(r"\D", "", row.get("Account #") or "")
            if acct:
                cur = out.setdefault(acct, {"due": 0.0, "appraisal": 0.0})
                cur["due"] += _money(row.get("Total Due")) or 0
                cur["appraisal"] = max(cur["appraisal"], _money(row.get("Appraisal Value")) or 0)
    return out


def _build_bell_market(src: Path) -> dict:
    import openpyxl  # noqa: PLC0415
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    ws = next(s for s in wb.worksheets if s.max_row and s.max_row > 1)
    rows = ws.iter_rows(values_only=True)
    col = {h: i for i, h in enumerate(next(rows)) if h is not None}
    out = {}
    for r in rows:
        pid, mkt = r[col["prop_id"]], r[col["market"]]
        if pid and mkt:
            out[str(int(pid))] = max(out.get(str(int(pid)), 0), float(mkt))
    wb.close()
    return out


def county_value(rec: dict, tcad: "_Tcad | None" = None) -> dict:
    """{'market', 'taxes_due', 'account', 'source'} - the CAD market value and taxes owed.

    Uses the full county research when it exists, else ONE light lookup
    (cached): TCAD API + Travis delinquent roll, the Williamson tax page,
    the BellCAD appraisal export + the GSA tax page."""
    cd = county_data(rec) if rec.get("county") else {}
    if cd.get("market_value") is not None:
        return {"market": _money(cd.get("market_value")), "taxes_due": _money(cd.get("total_due") or 0) or 0.0,
                "account": cd.get("account") or "", "source": cd.get("source") or "county research"}
    key = VALUE_DIR / f"{rec['county']}_{re.sub(r'[^A-Za-z0-9]', '', rec.get('apn') or '')}.json"
    hit = _load(key)
    if hit is not None:
        return hit
    out = {"market": None, "taxes_due": None, "account": "", "source": ""}
    try:
        if rec["county"] == "Travis":
            prop = (tcad or _Tcad()).property(rec["apn"]) or {}
            acct = prop.get("refID2") or ""
            due = _indexed(ROOT / "data" / "travis_tax_cache" / "TaxDelqOpenData.csv",
                           CACHE_DIR / "travis_delq_due.json", _build_travis_due).get(re.sub(r"\D", "", acct)) or {}
            out = {"market": _money(prop.get("marketValue")) if prop.get("marketValue") is not None else None,
                   "taxes_due": due.get("due", 0.0), "account": acct, "source": "TCAD + Travis delinquent roll"}
        elif rec["county"] == "Williamson":
            page = _http().get(f"{_WIL}/Property-Detail?PropertyQuickRefID={rec['apn'].strip().upper()}",
                               timeout=60).text
            mv = re.search(r"Total Market Value[^$]*\$([\d,]+)", _html_text(page))
            td = re.search(r'id="[^"]*tdPMTotalDue"[^>]*>(.*?)</td>', page, re.S)
            out = {"market": _money(mv.group(1)) if mv else None,
                   "taxes_due": (_money(_html_text(td.group(1))) or 0.0) if td else None,
                   "account": rec["apn"].strip().upper(), "source": "tax.wilcotx.gov"}
        elif rec["county"] == "Bell":
            import bell_gsa  # noqa: PLC0415
            pid = str(int(re.sub(r"\D", "", rec["apn"]) or 0))
            mkt = _indexed(ROOT / "data" / "bell_tax_cache" / "BellCAD_Appraisal_Data_Condensed_latest.xlsx",
                           CACHE_DIR / "bell_market.json", _build_bell_market).get(pid)
            bal = (bell_gsa.fetch_tax(pid) or {}).get("balance")
            out = {"market": mkt, "taxes_due": bal, "account": pid, "source": "BellCAD export + GSA tax page"}
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:160]
        logger.warning("value %s %s: %s", rec["county"], rec.get("apn"), e)
        return out   # not cached: retried next run
    _save(key, out)
    return out


def mortgage_estimate(rec: dict) -> tuple[float | None, str]:
    """DataSift equity % on its own estimate value -> estimated open mortgage balance."""
    ev = _money(rec.get("estimate_value"))
    eq = rec.get("equity_percent")
    if rec.get("open_mortgages") == 0:
        return 0.0, "free and clear (DataSift: no open mortgages)"
    if eq in (None, "") or not ev:
        if rec.get("source") == "county_roll":
            return None, "mortgage unknown (not in DataSift yet - check the clerk's deeds of trust)"
        return None, "mortgage unknown (no DataSift equity estimate)"
    eq = float(eq)
    if eq >= 99.5:
        return 0.0, "free and clear (DataSift equity 100%)"
    bal = round(ev * (1 - eq / 100))
    return float(bal), f"est. mortgage ${bal:,.0f} (DataSift: {eq:.0f}% equity on ${ev:,.0f})"


def arrears(rec: dict, value: dict, clerk_open: list[dict] | None = None) -> dict:
    """Everything owed against the property: [{kind, label, amount|None, source}], known total, warnings."""
    items = []
    taxes = value.get("taxes_due") if value.get("taxes_due") is not None else _money(rec.get("tax_delinquent_value"))
    items.append({"kind": "taxes", "label": "back taxes", "amount": taxes or 0.0, "source": value.get("source")})
    bal, why = mortgage_estimate(rec)
    items.append({"kind": "mortgage", "label": why, "amount": bal, "source": "DataSift"})
    warn = []
    if rec.get("foreclosure_date") or rec.get("auction_date"):
        warn.append(f"LOOK OUT: in foreclosure (DataSift: filed {rec.get('foreclosure_date') or '?'}, sale "
                    f"{rec.get('auction_date') or '?'}) - past due on the mortgage; the payoff is unknown but "
                    f"higher than the balance (arrears, fees)")
    if rec.get("lien_type"):
        warn.append(f"LOOK OUT: DataSift shows a {rec['lien_type']} lien (recorded {rec.get('lien_recording_date') or '?'})"
                    f" - amount unknown")
    if rec.get("bankruptcy_recording_date"):
        warn.append(f"LOOK OUT: bankruptcy recorded {rec['bankruptcy_recording_date']} - automatic stay / trustee")
    hot = [x for x in rec.get("lists") or [] if DISTRESS_LISTS.search(x)]
    if hot:
        warn.append(f"LOOK OUT: also on {', '.join(hot)} in DataSift")
    for e in clerk_open or []:
        if e["class"] == "mortgage" and bal is not None:
            label = f"open {e['doc_type'].lower()} {e['date']} ({e.get('creditor') or 'lender'}) - in the DataSift estimate"
            items.append({"kind": "clerk_mortgage", "label": label, "amount": None, "covered": True,
                          "source": e["url"]})
            continue
        label = (f"{LABEL_FOR.get(e['class'], e['class'])}: {e['doc_type']} {e['date']} "
                 f"{('(' + e['creditor'] + ')') if e.get('creditor') else ''} #{e['instrument']}"
                 + (" - name match only, verify it is this owner" if e.get("on_property") == "unknown" else ""))
        items.append({"kind": e["class"], "label": label, "amount": None, "source": e["url"]})
        if e["class"] == "foreclosure" and e.get("on_property", "same") == "same":
            warn.append(f"LOOK OUT: {e['doc_type'].lower()} recorded {e['date']} - foreclosure started; "
                        f"they are past due on the mortgage")
    if bal == 0.0 and any(e["class"] == "mortgage" and e.get("on_property", "same") == "same" for e in clerk_open or []):
        warn.append("LOOK OUT: DataSift says free and clear, but the clerk shows an unreleased deed of trust")
    known = round(sum(i["amount"] or 0 for i in items), 2)
    unknown = [i for i in items if i["amount"] is None and not i.get("covered")]
    return {"items": items, "known_total": known, "unknown": unknown, "warnings": warn}


def gate1(rec: dict, value: dict) -> dict:
    a = arrears(rec, value)
    if rec.get("source") == "county_roll" and value.get("taxes_due") == 0:
        # the Bell roll is from July: the live tax page says it has been paid since
        return {"pass": False, "reason": f"no taxes owed today ({value.get('source')}) - paid since the roll",
                "value": value.get("market"), "value60": None, "spread": None, "arrears": a}
    mkt = value.get("market")
    if not mkt:
        return {"pass": False, "reason": "no CAD market value - hold for a manual value", "value": None,
                "value60": None, "spread": None, "arrears": a}
    v60 = round(mkt * AS_IS_FACTOR)
    spread = round(v60 - a["known_total"])
    ok = spread >= GATE_MIN
    return {"pass": ok, "value": mkt, "value60": v60, "spread": spread, "arrears": a,
            "reason": (f"spread ${spread:,.0f} >= ${GATE_MIN:,.0f}" if ok else
                       f"spread ${spread:,.0f} < ${GATE_MIN:,.0f} (60% of ${mkt:,.0f} CAD minus "
                       f"${a['known_total']:,.0f} known arrears)")}


def stage_gate1(limit: int = 0) -> None:
    cands = _load(CANDIDATES, {}) or {}
    tcad = _Tcad()
    n = passed = 0
    for u, rec in cands.items():
        if limit and n >= limit:
            break
        if not rec.get("apn"):
            rec["gate1"] = {"pass": False, "reason": "no APN in DataSift", "spread": None}
            continue
        v = county_value(rec, tcad)
        rec["value"] = v
        rec["gate1"] = gate1(rec, v)
        n += 1
        passed += rec["gate1"]["pass"]
        if n % 200 == 0:
            logger.info("gate 1: %d screened, %d pass", n, passed)
            _save(CANDIDATES, cands)
    _save(CANDIDATES, cands)
    logger.info("gate 1: %d screened, %d pass (spread >= $%s)", n, passed, f"{GATE_MIN:,}")


def gate1_passed(rec: dict) -> bool:
    return bool((rec.get("gate1") or {}).get("pass"))


# ── GATE 2b: clerk encumbrances on the records with a curative problem ──

LABEL_FOR = {}   # filled from clerk_records.LABELS on first use


def clerk_jobs(rec: dict, deeds: list[dict]) -> list[tuple[str, str, str]]:
    """(county, person on title, date they took title) - at most two people per record."""
    people = [n for n in county_people(rec, county_data(rec), deeds)[:4]
              if _name_parts(n) and not is_entity(n) and not re.search(r"\b(COUNTY|TRUST|LIVING)\b", n, re.I)]
    seen, jobs = set(), []
    for n in people:
        p = _name_parts(n)
        if (p[0], p[2]) in seen or len(seen) >= 2:
            continue
        seen.add((p[0], p[2]))
        took = min((d.get("date") or "" for d in deeds if _same_person(n, d.get("grantee") or "")), default="")
        jobs.append((rec["county"], n, took or ""))
    return jobs


_NO_LEGAL_RE = re.compile(r"SEE INSTRUMENT|NO LEGAL|CAUSE|CASE NO|COSTS|^\W*\$|^#")
_LEGAL_STOP = {"LOT", "LOTS", "BLOCK", "BLK", "SEC", "SECTION", "PHASE", "ADDN", "ADDITION", "SUBD", "SUBDIVISION",
               "AMENDED", "AMD", "PLAT", "REPLAT", "RESUB", "UNIT", "PERM", "TEMP", "WEST", "EAST", "NORTH", "SOUTH",
               "TEXAS", "AUSTIN", "PART", "ACRES", "ACRE", "SURVEY", "ABSTRACT", "THE", "ESTATES", "COUNTY"}


def _lot_block(legal: str) -> tuple[str, str]:
    lot = re.search(r"\b(?:LOTS?|LT|L)\s*(?:NO\.?\s*)?(\d+[A-Z]?)\b", legal)
    blk = re.search(r"\b(?:BLOCK|BLK|BK|B)\s*(?:NO\.?\s*)?\"?([A-Z]?\d*[A-Z]?)\b", legal)
    return (lot.group(1).lstrip("0") if lot else ""), (blk.group(1).lstrip("0") if blk else "")


def legal_relation(clerk_legal: str, prop_legal: str, street: str) -> str:
    """Is a clerk instrument on this property? 'same', 'other', or 'unknown' (no usable legal).

    A clerk search is by NAME, so a hit is someone of that name until its legal says otherwise: on the
    2026-10-30 Top lists most "open liens" were deeds of trust on other subdivisions (Carmen Garcia had
    a dozen, all different lots, none on 2611 Francisco)."""
    c = " ".join(re.sub(r"[^A-Z0-9 ]", " ", (clerk_legal or "").upper()).split())
    if not c or _NO_LEGAL_RE.search((clerk_legal or "").upper()):
        return "unknown"
    m = re.match(r"\s*(\d+)\s+(.+)", street or "")
    words = [w for w in re.findall(r"[A-Z0-9]+", (m.group(2) if m else "").upper()) if len(w) >= 3] if m else []
    if m and words and re.search(rf"\b{m.group(1)}\s+(?:[NSEW]\s+)?{re.escape(words[0])}", c):
        return "same"
    loc = re.search(r"\bLOC\s+(\d+)\b", c)
    if m and loc and loc.group(1) != m.group(1):
        return "other"
    p = " ".join(re.sub(r"[^A-Z0-9 ]", " ", (prop_legal or "").upper()).split())
    if not p:
        return "unknown"
    toks = lambda x: {t for t in re.findall(r"[A-Z]{4,}", x)} - _LEGAL_STOP  # noqa: E731
    shared = toks(c) & toks(p)
    (cl, cb), (pl, pb) = _lot_block(c), _lot_block(p)
    outlot = lambda x: re.search(r"\bOLTS?\s+(\d+(?:\s+\d+)*)", x)  # noqa: E731
    if cl and cl == pl and cb == pb and outlot(c) and outlot(p) and outlot(c).group(1) == outlot(p).group(1):
        return "same"   # old Austin legals name the outlot, not a subdivision
    if toks(c) and toks(p) and not shared:
        return "other"
    if cl and pl and cl != pl:
        return "other"
    if cb and pb and cb != pb:
        return "other"
    return "same" if shared else "unknown"


def clerk_open_for(rec: dict, deeds: list[dict], legal: str = "", died: str = "") -> tuple[list[dict], list[str]]:
    """Open encumbrances from the cached clerk searches + notes (truncated / not searched).

    Kept: anything whose legal is this property, plus name-only items (no usable legal: a judgment or
    tax lien attaches by name) unless the name is too common or they post-date the owner's death."""
    import clerk_records as cr  # noqa: PLC0415
    LABEL_FOR.update(cr.LABELS)
    found, notes = [], []
    elsewhere = 0
    jobs = clerk_jobs(rec, deeds)
    if not jobs:
        return [], []
    for county, name, since in jobs:
        res = cr.cached(county, name, since)
        if res is None:
            notes.append(f"clerk records not searched yet for {name}")
            continue
        if res.get("truncated"):
            notes.append(f"{name}: {res['total']} clerk hits - name too common for a name search; check by "
                         f"legal description / instrument")
        kept = []
        for e in cr.encumbrances(res["rows"], name, since):
            on = legal_relation(e.get("legal") or "", legal, rec.get("street") or "")
            if on == "other" or (on == "unknown" and (res.get("truncated") or (died and (e["date"] or "") > died))):
                elsewhere += 1
                continue
            kept.append({**e, "on_property": on})
        encs = kept
        for e in encs:
            if e["released"]:
                notes.append(f"released: {e['doc_type']} {e['date']} ({e.get('creditor') or '?'}) by {e['released_by']}")
        found += cr.open_items(encs)
        for e in cr.stale_items(encs):
            notes.append(f"aged out (no release recorded): {e['doc_type']} {e['date']} ({e.get('creditor') or '?'}) "
                         f"#{e['instrument']} - nothing owed, but title may want a release")
    if elsewhere:
        notes.append(f"{elsewhere} recorded item(s) under this name are not tied to this property (another lot, "
                     f"or name-only and not credible) - not counted")
    uniq = {e["instrument"]: e for e in found}
    return list(uniq.values()), notes


def stage_clerk(limit: int = 0, only: str = "") -> None:
    import clerk_records as cr  # noqa: PLC0415
    cands = _load(CANDIDATES, {}) or {}
    obits = _load(CACHE_DIR / "obituary.json", {}) or {}
    verify = _load(CACHE_DIR / "verify.json", {}) or {}
    jobs = []
    for u, rec in cands.items():
        cd = county_data(rec)
        if not gate1_passed(rec) or not cd:
            continue
        a = assess(rec, cd, obits.get(u) or {}, probate_cases(rec, cd), verify, court_cases(rec, cd))
        if a["problem"] and (a["spread"] or 0) >= GATE_MIN:
            jobs += [j for j in clerk_jobs(rec, a["deeds"]) if cr.cached(*j) is None]
    jobs = [j for j in dict.fromkeys(jobs) if not only or j[0].lower() == only.lower()]
    if limit:
        jobs = jobs[:limit]
    logger.info("clerk: %d name searches queued", len(jobs))
    cr.search_people(jobs)


def fetch_legal(rec: dict, tcad: "_Tcad", s) -> str:
    """The property's own legal description from its county's public page."""
    county, apn = rec.get("county"), str(rec.get("apn") or "").strip()
    if county == "Travis":
        return ((tcad.property(apn) or {}).get("legalDescription") or "").strip()
    if county == "Williamson":
        page = s.get(f"{_WIL}/Property-Detail?PropertyQuickRefID={apn.upper()}", timeout=60).text
        m = re.search(r'id="[^"]*tdGILegalDescription"[^>]*>(.*?)</td>', page, re.S)
        return re.sub(r"^\W*Legal Description:?\s*", "", " ".join(_html_text(m.group(1)).replace("|", " ").split()), flags=re.I) if m else ""
    import bell_gsa  # noqa: PLC0415
    p = bell_gsa.fetch_parcel(apn)
    return (p.get("legal") or p.get("subdivision") or "").strip()


def stage_legal() -> None:
    """Legal description of every record that has clerk hits to check against it. Free, plain HTTP, cached."""
    cands = _load(CANDIDATES, {}) or {}
    obits = _load(CACHE_DIR / "obituary.json", {}) or {}
    verify = _load(CACHE_DIR / "verify.json", {}) or {}
    cache_p = CACHE_DIR / "legal.json"
    cache = _load(cache_p, {}) or {}
    tcad, s = _Tcad(), _http()
    n = fails = 0
    for u, rec in cands.items():
        cd = county_data(rec)
        key = f"{rec.get('county')}|{rec.get('apn')}"
        if not cd or key in cache or not gate1_passed(rec):
            continue
        a = assess(rec, cd, obits.get(u) or {}, probate_cases(rec, cd), verify, court_cases(rec, cd))
        if not a["problem"]:
            continue
        try:
            cache[key] = {"legal": fetch_legal(rec, tcad, s)}
        except Exception as e:  # noqa: BLE001
            fails += 1
            logger.warning("legal %s: %s", key, str(e)[:120])
            continue
        n += 1
        if n % 20 == 0:
            _save(cache_p, cache)
            logger.info("legal: %d fetched", n)
        time.sleep(0.4)
    _save(cache_p, cache)
    logger.info("legal: %d fetched, %d failed, %d with no legal", n, fails, sum(1 for v in cache.values() if not v["legal"]))


# ── stage 5: classify + score + report ───────────────────────────────

LENDER_RE = re.compile(
    r"^@|MORTGAGE|\bMTG\b|BANK|CREDIT UNION|\bF\.?C\.?U\b|SERVIC|TAX SERV|\bRETS\b|CORELOGIC|COTALITY|LERETA|"
    r"FIRST AMERICAN|ESCROW|LOAN|LENDING|FINANCIAL|NATIONSTAR|MR\.? COOPER|ROCKET|QUICKEN|PENNYMAC|USAA|"
    r"NAVY FED|LAKEVIEW|NEWREZ|SHELLPOINT|CARRINGTON|OCWEN|\bPHH\b|LOANCARE|CENLAR|DOVENMUEHLE|FLAGSTAR|"
    r"TRUIST|REGIONS|\bFROST\b|\bPNC\b|CITI|WELLS FARGO|CHASE|\bHUD\b|VETERANS|\bVA\b", re.I)
TITLE_RE = re.compile(r"TITLE|TITILE|ABSTRACT|CLOSING|SETTLEMENT", re.I)
TAXLOAN_RE = re.compile(r"TAX (LOAN|LIEN|FUND)|PROPEL|TAX ?EASE|DIMONT|OVATION|JUBILEE|PROPERTY TAX", re.I)
DEATH_DEED_RE = re.compile(r"DEATH|HEIRSHIP|EXECUTOR|ADMINISTRAT|PROBATE|MUNIMENT|SURVIVORSHIP|WILL\b|ESTATE", re.I)
TOD_RE = re.compile(r"TRANSFER ON DEATH|\bTOD\b", re.I)


def classify_payer(payer: str, owner_groups: list[str], prior_groups: list[str]) -> str:
    p = (payer or "").strip()
    if not p or p.startswith("?"):
        return "unknown"
    if p.upper() in ("OWNER", "CASH", "CHECK"):
        return "owner"
    if TITLE_RE.search(p):
        return "title_company"
    if TAXLOAN_RE.search(p):
        return "tax_lender"
    if LENDER_RE.search(p):
        return "lender_escrow"
    pt = _tokens(p)
    squashed = re.sub(r"[^A-Z]", "", p.upper())

    def match(groups: list[str]) -> str:
        best = ""
        for g in groups:
            toks = list(_tokens(to_namelf(g)))
            if not toks:
                continue
            sur = to_namelf(g).split()[0].upper() if to_namelf(g) else ""
            given = {t for t in toks if t != sur}
            sur_hit = sur and (sur in pt or (len(sur) >= 4 and sur in squashed))
            if sur_hit and given & pt:
                return "same"
            if sur_hit:
                best = "surname"
        return best

    m = match(owner_groups)
    if m == "same":
        return "owner"
    mp = match(prior_groups)
    if mp == "same":
        return "prior_owner"
    if is_entity(p):
        return "entity"
    if m == "surname" or mp == "surname":
        return "same_surname_relative"
    return "other_individual"


def _years_between(a: str, b: str) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days / 365.25
    except (TypeError, ValueError):
        return None


HEIRSHIP_RE = re.compile(r"HEIRSHIP|DEATH CERT|SURVIVORSHIP", re.I)
BREAK_OK_RE = re.compile(r"TRUSTEE|SHERIFF|CONSTABLE|TAX|FORECLOS|EXECUTOR|ADMINISTRAT|PROBATE|MUNIMENT|"
                         r"SUBDIVISION|PLAT|CORRECTION|DEATH|HEIRSHIP|RECEIVER|GUARDIAN|DIVORCE|DECREE|JUDGMENT", re.I)


def _same_person(first_last: str, text: str) -> bool:
    """'Marsha Jo Kasper' vs a payer/grantor string in any orientation."""
    parts = [t for t in re.findall(r"[A-Z]{2,}", first_last.upper()) if t not in _NAME_NOISE]
    if len(parts) < 2:
        return False
    given, sur = parts[0], parts[-1]
    toks = _tokens(text)
    squashed = re.sub(r"[^A-Z]", "", (text or "").upper())
    sur_hit = sur in toks or (len(sur) >= 4 and sur in squashed)
    given_hit = given in toks or (len(given) >= 4 and given in squashed)
    return sur_hit and given_hit


def _shares_name(a: str, b: str) -> bool:
    ta = {t for t in _tokens(a) if len(t) >= 3}
    tb = {t for t in _tokens(b) if len(t) >= 3}
    return bool(ta & tb)


_SUFFIXES = {"JR", "SR", "II", "III", "IV"}
GRANTEE_TRUNC = 34   # Williamson deed-table name fields are cut at ~35 characters


def _name_parts(n: str):
    toks = [t for t in re.findall(r"[A-Z']+", (n or "").upper()) if t not in ("ESTATE", "OF", "THE", "DECEASED")]
    suf = next((t for t in toks if t in _SUFFIXES), "")
    toks = [t for t in toks if t not in _SUFFIXES]
    if len(toks) < 2:
        return None
    return _GIVEN_ALIASES.get(toks[0], toks[0]), toks[1:-1], toks[-1], suf


def _surname_set(p) -> set[str]:
    """Last name + a second Hispanic surname just before it ('Maria Garcia Lopez' -> GARCIA, LOPEZ)."""
    out = {p[2]}
    if p[1] and p[1][-1] in HISPANIC_SURNAMES and p[1][-1] not in _GIVEN_NOT_SURNAME:
        out.add(p[1][-1])
    return out


def name_match(a: str, b: str) -> str:
    """'strong' (first+last AND a middle initial or suffix agree), 'weak'
    (first+last only), or 'no' (different person, incl. conflicting middle/suffix).

    Two-surname names match on either surname ('Maria Garcia Lopez' = 'Maria Garcia');
    particles (DE, LA, DEL) and the other surname never count as a middle initial."""
    pa, pb = _name_parts(a), _name_parts(b)
    if not pa or not pb or pa[0] != pb[0]:
        return "no"
    sa, sb = _surname_set(pa), _surname_set(pb)
    if not (sa & sb):
        return "no"
    # both carry a second surname and they differ: 'Rogelio Cruz Aguilar' is not 'Rogelio Vallejo
    # Aguilar' (the maternal surname tells two namesakes apart)
    if (sa - sb - NAME_PARTICLES) and (sb - sa - NAME_PARTICLES):
        return "no"
    skip = NAME_PARTICLES | sa | sb
    ma = [m for m in pa[1] if m not in skip]
    mb = [m for m in pb[1] if m not in skip]
    strong = False
    if ma and mb:
        if ma[0][0] != mb[0][0]:
            return "no"
        strong = True
    if pa[3] and pb[3]:
        if pa[3] != pb[3]:
            return "no"
        strong = True
    return "strong" if strong else "weak"


def _case_decedent(case: dict) -> str:
    """'Estate Of James D. Hall, II, Deceased' -> 'James D. Hall, II'; '' for civil cases."""
    m = re.match(r"\s*(?:In the )?Estate Of\s+(.+?)(?:,?\s*Deceased.*)?$", case.get("style") or "", re.I)
    if not m:
        return ""
    return re.split(r"\s+(?:a/?k/?a|also known as)\s+", m.group(1), flags=re.I)[0].strip(" ,")


def obit_person(rec: dict, cd: dict, obit: dict) -> str:
    """Whom the obituary is about. The engine often leaves decedent_name blank;
    the obituary URL slug ('.../lee-medlock-obituary') then says which of the
    co-owners it is - never assume the first name searched (often the survivor)."""
    nm = ((obit or {}).get("decedent_name") or "").strip()
    if nm:
        return nm
    url = (obit or {}).get("obituary_url") or ""
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].lower()
    slug = set(re.findall(r"[a-z]{2,}", path))
    if slug:
        people = county_people(rec, cd, sorted(cd.get("deeds") or [], key=lambda d: d.get("date") or "",
                                               reverse=True))
        for n in people:
            pa = _name_parts(n)
            if pa and pa[0].lower() in slug and pa[2].lower() in slug:
                return n
        # the URL names someone, and it is nobody on title
        tail = next((seg for seg in reversed(path.split("/")) if re.search(r"[a-z]{3,}-[a-z]{2,}", seg)), "")
        tail = re.sub(r"-?(obituary|obit)\b|-\d+$", "", tail)
        if tail:
            return " ".join(w for w in tail.split("-") if w and not w.isdigit()).title()
    for raw in ((obit or {}).get("searched_name"), cd.get("owner_raw")):
        names = person_names(raw or "")
        if names:
            return names[0]
    if rec.get("owner_first") and rec.get("owner_last"):
        return f"{rec['owner_first']} {rec['owner_last']}".title()
    return ""


def took_title_after(name: str, deeds: list[dict], after: str) -> dict | None:
    """A deed dated after `after` that conveys TO `name` as a living person.

    A dead person cannot buy: an obituary/probate hit contradicted by this is
    almost always a same-named stranger; a land-record death contradicted by it
    is a defect (deed to a dead person)."""
    if not after:
        return None
    for d in deeds:
        if (d.get("date") or "") <= after or HEIRSHIP_RE.search(d.get("type") or "") \
                or DEATH_DEED_RE.search(d.get("type") or ""):
            continue
        g = d.get("grantee") or ""
        segs = [s.strip() for s in re.split(r"\s*&\s*", g) if s.strip()]
        if len(g) >= GRANTEE_TRUNC and len(segs) > 1:
            segs = segs[:-1]   # Williamson cuts the field at 35 chars: '... & THE WALLACE'
        sur = to_namelf(segs[0]).split()[0] if segs and to_namelf(segs[0]) else ""
        for s in segs:
            if ESTATE_RE.search(s) or re.match(r"THE\s", s, re.I):
                continue
            if _same_person(name, s) or (len(s.split()) <= 2 and sur and _same_person(name, f"{s} {sur}")):
                return d
    return None


def _inherited_after(name: str, deeds: list[dict], died: str, grace_years: int = 2) -> dict | None:
    """A will / executor / heirship conveyance TO `name` recorded well after their supposed death.
    A beneficiary who died first takes nothing (the gift lapses), so Carolyn Ward receiving Gertrude's
    house by will in 2023 means the 2019 Carolyn Ward obituary is a namesake. The grace covers late
    recording of an estate that was already open."""
    if not died or len(died) < 4 or not died[:4].isdigit():
        return None
    cutoff = f"{int(died[:4]) + grace_years}{died[4:]}"
    for d in deeds:
        if (d.get("date") or "") > cutoff and DEATH_DEED_RE.search(d.get("type") or "") \
                and not _same_person(name, d.get("grantor") or "") \
                and any(_same_person(name, s) for s in re.split(r"\s*&\s*", d.get("grantee") or "")):
            return d
    return None


def county_people(rec: dict, cd: dict, deeds: list[dict]) -> list[str]:
    """Everyone the county records put on title: owner of record + the last two grantees."""
    owner_raw = cd.get("owner_raw") or ""
    county_names = []
    for raw in [owner_raw] + recent_grantees(deeds, owner_raw):
        # full-middle reading of each NAMELF part ('CLEVELAND JANIE EARLINE
        # TAYLOR' -> 'Janie Earline Taylor Cleveland'): the shared parser drops
        # middles, and a middle initial is what tells two same-named people apart
        raw = _amp(raw)
        segs = re.split(r"\s*&\s*", raw)
        for i, seg in enumerate(segs):
            toks = to_namelf(seg).split()
            # a later estate segment may be prose order (see owner_segments), and so is Bell's
            # 'LAST, FIRST & FIRST2 M LAST2' co-owner (read by extra_readings via person_names)
            prose = _ESTATE_OF_SEG_RE.match(seg) or _THE_X_ESTATE_RE.match(seg) or _LAST_ESTATE_OF_RE.match(seg) \
                or (i > 0 and ESTATE_RE.search(seg)) or (i > 0 and "," in segs[0] and "," not in seg) \
                or (i > 0 and _PROSE_MI_RE.match(seg))
            if len(toks) >= 3 and not prose and not is_entity(seg):
                given, sur = split_namelf(toks)
                full = " ".join(given + sur).title() if given else ""
                if full and full not in county_names:
                    county_names.append(full)
        county_names += [n for n in person_names(raw) if n not in county_names]
    if not county_names and rec.get("owner_first") and rec.get("owner_last"):
        county_names = [f"{rec['owner_first']} {rec['owner_last']}".title()]
    return county_names


def grade_probate(rec: dict, cd: dict, obit: dict, cases: list[dict], deeds: list[dict]):
    """Split name-searched probate cases into (accepted, possible, ruled_out).

    The docket is searched by first+last name, so a hit is a different person
    until shown otherwise: accepted = middle initial / suffix agree with the
    county name, or a first+last hit corroborated by the obituary's DOD or an
    estate label for the same person; ruled out = conflicting middle/suffix, a
    civil (non-estate) case, or the person took title AFTER that death."""
    owner_raw = cd.get("owner_raw") or ""
    county_names = county_people(rec, cd, deeds)
    estate_names = [n for n, est in owner_segments(owner_raw) if est]
    for d in deeds:
        estate_names += [n for n, est in owner_segments(d.get("grantee") or "") if est]
    obit_dod = (obit or {}).get("date_of_death") or "" if (obit or {}).get("owner_deceased") == "yes" else ""
    accepted, possible, ruled_out = [], [], []
    for c in cases:
        dec = _case_decedent(c)
        if not dec:
            ruled_out.append(f"{c.get('case_number')}: not an estate case")
            continue
        grades = [(name_match(dec, n), n) for n in county_names]
        best = next((g for g in grades if g[0] == "strong"), None) or next((g for g in grades if g[0] == "weak"), None)
        if not best:
            ruled_out.append(f"{c.get('case_number')}: Estate of {dec} - name conflicts with the county owner")
            continue
        born, pby = owner_birth_year(rec), c.get("party_birth_year")
        crm = f"{rec.get('owner_first') or ''} {rec.get('owner_last') or ''}"
        if born and pby and abs(born - pby) > 3 and name_match(dec, crm) != "no":
            ruled_out.append(f"{c.get('case_number')}: Estate of {dec} - court party born {pby}, the owner ~{born}")
            continue
        died = c.get("date_of_death") or c.get("filed") or ""
        bought = took_title_after(best[1], deeds, died)
        if bought:
            ruled_out.append(f"{c.get('case_number')}: Estate of {dec} (died by {died}) - but {best[1]} took "
                             f"title {bought['date']}, so a different person")
            continue
        gap = _years_between(obit_dod, died) if obit_dod and died else None
        corroborated = (gap is not None and abs(gap) * 365.25 <= 45) or \
            any(name_match(dec, e) != "no" for e in estate_names)
        (accepted if best[0] == "strong" or corroborated else possible).append(c)
    # Two estates for the same first+last (two Eleanor A. Smiths) - one person can only die once: keep the
    # one whose court birth year fits the owner, else the newest; the rest become "possible".
    by_person: dict = {}
    for c in accepted:
        p = _name_parts(_case_decedent(c))
        by_person.setdefault((p[0], p[2]) if p else c["case_number"], []).append(c)
    born = owner_birth_year(rec)
    for group in by_person.values():
        if len(group) < 2:
            continue
        fit = [c for c in group if born and c.get("party_birth_year") and abs(born - c["party_birth_year"]) <= 3]
        keep = fit[0] if fit else max(group, key=lambda c: c.get("filed") or c.get("date_of_death") or "")
        for c in group:
            if c is not keep:
                accepted.remove(c)
                possible.append(c)
    return accepted, possible, ruled_out


def dead_people(rec: dict, cd: dict, obit: dict, cases: list[dict], deeds: list[dict],
                memorials: list[dict] | None = None) -> list[dict]:
    """Everyone we have evidence is dead: {name 'First Last', dead_by date or '', source}.

    dead_by is the date they were dead BY - an exact DOD when an obituary or
    the court has one, otherwise the recording date of a death certificate /
    heirship affidavit (a recorded death certificate proves death before it).
    """
    people: dict[str, dict] = {}

    def add(name: str, dead_by: str, source: str) -> None:
        key = name.lower()
        cur = people.get(key)
        if cur is None:
            people[key] = {"name": name, "dead_by": dead_by, "sources": [source]}
            return
        if source not in cur["sources"]:
            cur["sources"].append(source)
        if dead_by and (not cur["dead_by"] or dead_by < cur["dead_by"]):
            cur["dead_by"] = dead_by

    owner_raw = cd.get("owner_raw") or ""
    if (obit or {}).get("owner_deceased") == "yes":
        nm = obit_person(rec, cd, obit)
        if nm:
            add(nm, obit.get("date_of_death") or "", "obituary")
    for c in cases:
        nm = _case_decedent(c)
        if nm:
            add(nm.title(), c.get("date_of_death") or c.get("filed") or "", f"probate {c.get('case_number')}")
    for m in memorials or []:
        add(m["person"], m.get("death_date") or "", m.get("source") or f"Find A Grave #{m['id']}")
    # Estate label on the roll: the segment carrying EST/ESTATE OF is the decedent.
    for n, est in owner_segments(owner_raw):
        if est:
            add(n, "", "county roll estate label")
    for d in deeds:
        typ = d.get("type") or ""
        grantor, grantee = d.get("grantor") or "", d.get("grantee") or ""
        when = d.get("date") or ""
        if HEIRSHIP_RE.search(typ):
            for n in person_names(grantor):
                if not _same_person(n, grantee):
                    add(n, when, f"{typ.lower()} {when}")
        elif ESTATE_RE.search(grantor):
            for n, est in owner_segments(grantor):
                if est:
                    add(n, when, f"deed from the estate {when}")
        # a deed made TO '<X> ESTATE' means X was dead by then
        for n, est in owner_segments(grantee):
            if est and when:
                if len(grantee) >= GRANTEE_TRUNC:   # 'ESTATE OF MAURICE L' (field cut off)
                    *head, tail = n.upper().split()
                    known = next((q["name"] for q in people.values()
                                  if all(t in q["name"].upper().split() for t in head)
                                  and any(t.startswith(tail) for t in q["name"].upper().split())), "")
                    if not known:
                        continue
                    n = known
                add(n, when, f"deed to the estate {when}")
    if rec.get("owner_deceased") and rec.get("owner_first") and rec.get("owner_last"):
        add(f"{rec['owner_first']} {rec['owner_last']}".title(), "", "DataSift deceased flag")
    return list(people.values())


def chain_defects(deeds_newest_first: list[dict], dead: list[dict], owner_raw: str) -> list[str]:
    """Land-record things that don't add up (deed history only; Bell often hides names)."""
    out: list[str] = []
    asc = [d for d in reversed(deeds_newest_first) if d.get("date")]
    # 1. broken links: the next seller should be (part of) the last buyer
    # Same-day deeds come back in arbitrary order, and pre-1990 bank/builder/REO
    # paper is mostly indexing noise, so: compare against EVERY earlier-or-same-
    # day buyer, only flag individual-to-individual gaps since 1990.
    for i, cur in enumerate(asc):
        cg = cur.get("grantor") or ""
        if i == 0 or not cg or cur["date"] < "1990-01-01" or BREAK_OK_RE.search(cur.get("type") or ""):
            continue
        if is_entity(cg) or LENDER_RE.search(cg) or ESTATE_RE.search(cg) or LIFE_ESTATE_RE.search(cg):
            continue
        earlier = [d.get("grantee") or "" for d in asc if d["date"] <= cur["date"] and d is not cur]
        if not any(earlier):
            continue
        if not any(_shares_name(g, cg) for g in earlier if g):
            prev = next((d for d in reversed(asc[:i]) if d.get("grantee")), asc[i - 1])
            out.append(f"CHAIN BREAK: {cg} signed the {cur['date']} {(cur.get('type') or 'deed').lower()}, "
                       f"but no recorded deed ever put title in that name (last buyer: {prev.get('grantee')} "
                       f"{prev['date']}) - unrecorded transfer / wild deed?")
    # 1b. the roll's owner never shows up as a buyer at all
    named = [d for d in asc if d.get("grantee")]
    if named and owner_raw and not is_entity(owner_raw) and named[-1]["date"] >= "1990-01-01"             and not any(_shares_name(d["grantee"], owner_raw) for d in named):
        out.append(f"OWNER NOT IN THE DEED HISTORY: no recorded deed puts title in {owner_raw} (last recorded "
                   f"buyer {named[-1]['grantee']} {named[-1]['date']}) - unrecorded transfer / wild deed?")
    # 2. heirs hold fractional shares: heirship recorded, nothing conveyed since
    if asc:
        last = asc[-1]
        if HEIRSHIP_RE.search(last.get("type") or ""):
            out.append(f"HEIRS HOLD SHARES: {last['type'].lower()} {last['date']} is the last record - "
                       f"no deed from the heirs since, so every heir owns a fraction and must sign")
    if not is_entity(owner_raw) and (re.search(r"\bET\s*AL\b|\bHEIRS\b", owner_raw, re.I)
                                     or len([p for p in re.split(r"\s*&\s*", owner_raw) if p.strip()]) >= 3):
        out.append(f"FRACTIONAL OWNERS on the roll ({owner_raw}) - all must sign")
    # 3. deed signed by someone already dead
    for d in asc:
        for p in dead:
            if p["dead_by"] and d["date"] > p["dead_by"] and _same_person(p["name"], d.get("grantor") or "") \
                    and not ESTATE_RE.search(d.get("grantor") or "") and not BREAK_OK_RE.search(d.get("type") or "") \
                    and not re.search(r"WILL\b|MISC|AFFIDAVIT", d.get("type") or "", re.I):
                out.append(f"DEAD GRANTOR: {d['date']} {(d.get('type') or 'deed').lower()} is signed by "
                           f"{p['name']}, who was dead by {p['dead_by']} - possible forged/invalid deed")
    # 3b. deed made TO someone already dead (land-record deaths only reach
    # here contradicted; obituary/probate hits contradicted this way were
    # already ruled out as a different person)
    for p in dead:
        d = took_title_after(p["name"], asc, p["dead_by"])
        if d:
            out.append(f"DEED TO A DEAD PERSON: {d['date']} {(d.get('type') or 'deed').lower()} conveys to "
                       f"{p['name']}, who was dead by {p['dead_by']} ({'; '.join(p['sources'])}) - void grant / "
                       f"title still in the estate")
    # 4. transfer-on-death deed, owner died, nothing recorded since
    tods = [d for d in asc if TOD_RE.search(d.get("type") or "") or "(TOD)" in (d.get("grantee") or "").upper()]
    if tods:
        tod = tods[-1]
        after = [d for d in asc if d["date"] > tod["date"]]
        owners_dead = [p for p in dead if _same_person(p["name"], tod.get("grantor") or "")
                       or _same_person(p["name"], tod.get("grantee") or "") or _same_person(p["name"], owner_raw)]
        if owners_dead and not after:
            out.append(f"TOD NOT COMPLETED: transfer-on-death deed {tod['date']}; {owners_dead[0]['name']} has died "
                       f"but no affidavit of death / deed to the beneficiary has been recorded since")
    return list(dict.fromkeys(out))


def ghost_payments(pays: list[dict], dead: list[dict], county: str, living: list[str] | None = None) -> list[dict]:
    """Payments made in a dead person's name after they were dead.

    Not ghosts: paying AS the estate (incl. Williamson's run-together
    'LIVIE JR ESTGAULT'), a Jr/Sr of the same name, and lender/title payers.
    A payer name that also names a LIVING co-owner is the joint account still
    in use - recorded, but weak."""
    hits = []
    for p in pays:
        payer = p.get("payer") or ""
        if not payer or payer.startswith("?") or p.get("class") in ("lender_escrow", "title_company", "tax_lender"):
            continue
        if ESTATE_RE.search(payer):   # paying AS the estate (executor) is legitimate
            continue
        squashed = re.sub(r"[^A-Z]", "", payer.upper())
        pay_suf = set(re.findall(r"\b(JR|SR|II|III|IV)\b", payer.upper()))
        for person in dead:
            if not _same_person(person["name"], payer):
                continue
            parts = _name_parts(person["name"])
            if parts and re.search(r"EST(ATE)?" + parts[2], squashed):
                break
            if pay_suf != ({parts[3]} - {""} if parts else set()):
                continue   # 'JOHN C MERCADO JR' is the son, not the dead John C Mercado
            if person["dead_by"] and (p.get("date") or "") <= person["dead_by"]:
                continue
            co = next((n for n in (living or []) if _same_person(n, payer)), "")
            if co:
                strength = f"weak (joint account name - living co-owner {co} is on it)"
            elif county == "Williamson" and p.get("payor_id") and p.get("payor_id") == p.get("owner_party_id"):
                strength = "weak (receipt prints the owner account name by default)"
            elif county == "Travis" and p.get("channel") in ("CRC", "MPW"):
                strength = "strong (name on the card/counter payment)"
            else:
                strength = "strong (separate payor record in that name)" if county == "Williamson" else "medium"
            hits.append({**p, "dead_person": person["name"], "dead_by": person["dead_by"] or "date unknown",
                         "strength": strength, "timing_known": bool(person["dead_by"])})
            break
    return hits


_TIER_ORDER = {"A": 0, "B": 1, "C": 2, "Drop": 3}


def order_of(tier: str) -> int:
    return _TIER_ORDER[tier]


def death_proof(cases: list[dict], death_deeds: list[dict], est_label: bool, memorials: list[dict],
                obit_yes: bool, death_index: bool = False) -> str:
    """How the death is proven. A court case or a recorded instrument is proof;
    a memorial or obituary is a human-written reference (Jordan John's rule)."""
    if cases or death_deeds:
        return "court/recorded"
    if death_index:
        return "death index (SSDI / state)"
    if est_label:
        return "county roll estate label"
    if memorials and obit_yes:
        return "obituary + memorial"
    if memorials:
        return "memorial only"
    if obit_yes:
        return "obituary only"
    return "none"


_OTHER_CASE_RE = re.compile(r"TAX CASE|DIVORCE|MARRIAGE|HEIRSHIP|GUARDIAN|PARTITION|TRESPASS|QUIET|TITLE|"
                            r"MUNIMENT|SMALL ESTATE|INDEPENDENT ADMIN|DEPENDENT ADMIN|PROBATE", re.I)
_CLOSED_RE = re.compile(r"DISPOSED|DISMISSED|CLOSED|FINAL|JUDGMENT|RELEASE", re.I)


def _style_names(style: str, first: str, last: str) -> list[str]:
    """Every 'First [M.] Last' in a case style that has this first+last."""
    pat = rf"\b{re.escape(first)}\s+(?:([A-Za-z])[a-z]*\.?\s+){{0,2}}{re.escape(last)}\b(?:,?\s+(Jr|Sr|II|III|IV)\b)?"
    return [m.group(0).replace(",", "") for m in re.finditer(pat, style, re.I)]


def grade_court_cases(rec: dict, cd: dict, deeds: list[dict], other: list[dict]) -> tuple[list, list, list]:
    """Non-estate Williamson cases naming someone on title -> (reasons, defects, notes).

    Tax suits filed while they owned it (an open one is the foreclosure clock);
    a divorce between two co-owners on the roll (who was awarded the house?);
    heirship / partition / title suits. Hits that conflict on a middle initial
    are dropped; first+last-only hits are notes, never points."""
    reasons, defects, notes = [], [], []
    people = county_people(rec, cd, deeds)
    for c in other or []:
        typ, style = c.get("type_status") or "", c.get("style") or ""
        # Odyssey Portal hits carry the party the search matched: a conflicting middle initial is a namesake
        if c.get("party") and people and all(name_match(c["party"], n) == "no" for n in people):
            continue
        texts = [style] + [p.get("name") or "" for p in c.get("parties") or []]
        hits = []
        for n in people:
            p = _name_parts(n)
            if not p:
                continue
            for s in (x for t in texts for x in _style_names(t, p[0], p[2])):
                g = name_match(s, n)
                if g != "no":
                    hits.append((n, g))
        if not hits:
            continue
        who = sorted({n for n, _ in hits})
        distinct = {_name_parts(n)[0] for n in who}   # two spellings of one person are not two owners
        strong = any(g == "strong" for _, g in hits) or len(distinct) >= 2
        if strong and not _CLOSED_RE.search(typ) and any(re.search(r"UNSERVED", e, re.I) for e in c.get("events") or []):
            notes.append(f"Unserved court papers in {c['case_number']} - the county can't find them either")
        acquired = min((d.get("date") or "" for d in deeds for n in who
                        if _same_person(n, d.get("grantee") or "")), default="")
        label = f"{c['case_number']} {style} ({c.get('filed') or '?'}; {typ})"
        open_case = not _CLOSED_RE.search(typ)
        if re.search(r"TAX CASE|\bTAX\b", typ, re.I):
            if acquired and (c.get("filed") or "") < acquired:
                continue   # a tax suit against a prior owner/namesake
            if open_case and strong:
                reasons.append(f"OPEN TAX SUIT {c['case_number']} filed {c.get('filed')} - the county is "
                               f"foreclosing; the clock is running")
            notes.append(("Tax suit: " if strong else "Possible tax suit (name only): ") + label)
        elif re.search(r"\b736\b|FORECLOS|HOME EQUITY", typ, re.I):
            if acquired and (c.get("filed") or "") < acquired:
                continue   # against a prior owner/namesake
            if open_case and strong:
                reasons.append(f"HOME-EQUITY FORECLOSURE {c['case_number']} filed {c.get('filed')} (Rule 736) - "
                               f"the lender is foreclosing; the clock is running")
            notes.append(("Home-equity foreclosure: " if strong else "Possible foreclosure (name only): ") + label)
        elif re.search(r"DIVORCE|MARRIAGE", typ + style, re.I):
            if len(distinct) >= 2:
                defects.append(f"DIVORCE BETWEEN CO-OWNERS: {label} - both still on title; pull the decree to see "
                               f"who was awarded the property")
            else:
                notes.append(("Divorce: " if strong else "Possible divorce (name only): ") + label)
        else:
            if re.search(r"PARTITION|TRESPASS|QUIET|TITLE", typ, re.I) and strong:
                defects.append(f"TITLE SUIT: {label}")
            else:
                notes.append(("Court case: " if strong else "Possible court case (name only): ") + label)
    return reasons, defects, notes


def vesting_date(deeds: list[dict], owner_raw: str) -> str:
    """When the owner of record took title by an ordinary deed from someone else ('' if they never did).
    A death before that belongs to a prior owner: the title passed on, it is not this owner's problem."""
    owners = person_names(owner_raw) if owner_raw else []
    for d in sorted(deeds, key=lambda d: d.get("date") or "", reverse=True):
        typ, g = d.get("type") or "", d.get("grantee") or ""
        if HEIRSHIP_RE.search(typ) or DEATH_DEED_RE.search(typ):
            continue
        if any(_same_person(o, g) for o in owners) and not any(_same_person(o, d.get("grantor") or "") for o in owners):
            return d.get("date") or ""
    return ""


def on_roll(name: str, owner_raw: str) -> bool:
    return any(name_match(name, o) != "no" or _same_person(name, o) for o in person_names(owner_raw))


def real_heirs(heirs: list, decedent: str = "") -> list[dict]:
    """The obituary parser files 'Estate of X' and fragments like 'Of Garza' as heirs. Those are not people."""
    out = []
    for h in heirs if isinstance(heirs, list) else []:
        n = (h.get("name") or "").strip()
        if not n or (h.get("relationship") or "").lower() == "estate" or re.match(r"(estate\s+)?of\b", n, re.I) \
                or (decedent and name_match(n, decedent) == "strong"):
            continue
        out.append(h)
    return out


def _obit_omits_applicant(case: dict, heirs: list[dict]) -> bool:
    """The court's applicant is nowhere among the obituary's people (a first name alone counts: 'Dorothy')."""
    pr = [t for t in re.findall(r"[A-Z]{3,}", (case.get("pr_name") or "").upper())]
    named = [re.findall(r"[A-Z]{3,}", (h.get("name") or "").upper()) for h in heirs]
    if not pr or not named:
        return False
    return not any((len(n) == 1 and n[0] == pr[0]) or len(set(n) & set(pr)) >= 2 for n in named)


def _common_name(rec: dict, deeds: list[dict]) -> bool:
    import clerk_records as cr  # noqa: PLC0415
    return any((cr.cached(*j) or {}).get("truncated") for j in clerk_jobs(rec, deeds))


def _recorded_death(name: str, deeds: list[dict]) -> str:
    """The date a deed to/from this person's estate shows they were already dead ('' if none)."""
    for d in sorted(deeds, key=lambda d: d.get("date") or ""):
        for side in (d.get("grantee") or "", d.get("grantor") or ""):
            # only the part labelled estate: in 'ROBLES ABEL & ESTATE OF DELFINA' Abel is the living one
            if d.get("date") and any(est and (_same_person(name, n) or name_match(name, n) != "no")
                                     for n, est in owner_segments(side)):
                return d["date"]
    return ""


def assess(rec: dict, cd: dict, obit: dict, cases: list[dict], verify: dict | None = None,
           other_cases: list[dict] | None = None) -> dict:
    today = date.today().isoformat()
    owner_raw = cd.get("owner_raw") or ""
    deeds = sorted(cd.get("deeds") or [], key=lambda d: d.get("date") or "", reverse=True)
    pays = sorted(cd.get("payments") or [], key=lambda p: p.get("date") or "", reverse=True)
    reasons, drops = [], []

    # ── death evidence (name-matched sources are checked against the deeds) ──
    cases, possible_cases, ruled_out = grade_probate(rec, cd, obit, cases, deeds)
    death_pts = 0
    obit = dict(obit or {})
    obit_conflict = ""
    if obit.get("owner_deceased") == "yes" and obit.get("date_of_death"):
        who = obit_person(rec, cd, obit)
        bought = took_title_after(who, deeds, obit["date_of_death"]) or _inherited_after(who, deeds, obit["date_of_death"])
        if bought:
            obit_conflict = (f"obituary ruled out: {who} died {obit['date_of_death']} per the obituary, but took "
                             f"title {bought['date']} ({bought.get('grantee')}) - a different person")
            obit["owner_deceased"] = ""
    settled = survivor_holds_title(deeds, owner_raw)
    # only a recently closed estate: Maydell Kelly inherited by probate in 2011 and her own undated
    # obituary (birth year matches) is the 2026 death that matters
    if obit.get("owner_deceased") == "yes" and not obit.get("date_of_death") and settled \
            and 0 <= (_years_between(settled.get("date") or "", date.today().isoformat()) or 0) <= 10:
        # an undated obituary on an heir who inherited through an estate is almost always the
        # decedent's (Saldana: executor's deed 2024 from Maria's estate to Abraham)
        obit_conflict = (f"undated obituary set aside: {settled['grantee'].strip()} inherited by "
                         f"{settled['type'].lower()} {settled['date']} - the obituary is probably "
                         f"{settled.get('grantor', '').strip() or 'the prior owner'}'s")
        obit["owner_deceased"] = ""
    household_death = ""
    if obit.get("owner_deceased") == "yes":
        on_title = county_people(rec, cd, deeds)
        dec = obit_person(rec, cd, obit)
        if on_title and not any(name_match(dec, n) != "no" or _same_person(dec, n) for n in on_title):
            # the spouse-obituary trap: a death in the household is not the owner's death
            household_death = (f"obituary is for {dec} (DOD {obit.get('date_of_death') or '?'}), who is not on "
                               f"title - a household death, not the owner's; check whether {dec} had an interest")
            obit["owner_deceased"] = ""
    verify = verify or {}
    # someone read this obituary and it is not the owner (see data/curative_audit/obit_review.json)
    review = (verify.get("obit_review") or {}).get(rec.get("uuid") or "") or {}
    if review.get("final_url"):
        obit["obituary_url"] = review["final_url"]
    if obit.get("owner_deceased") == "yes" and review.get("verdict") == "wrong":
        obit_conflict = f"obituary ruled out: {review.get('why')}"
        obit["owner_deceased"] = ""
    if obit.get("owner_deceased") == "yes" and obit.get("date_of_death"):
        who = obit_person(rec, cd, obit)
        estate_by = _recorded_death(who, deeds)
        if estate_by and estate_by[:4].isdigit() and int(obit["date_of_death"][:4]) - int(estate_by[:4]) > 2:
            obit_conflict = (f"obituary ruled out: it says {who} died {obit['date_of_death']}, but a deed shows their "
                             f"estate by {estate_by} - a namesake")
            obit["owner_deceased"] = ""
    if obit.get("owner_deceased") == "yes" and cases and _common_name(rec, deeds):
        try:
            named = real_heirs(json.loads(obit.get("heir_map_json") or "[]"))
        except (TypeError, ValueError):
            named = []
        if any(_obit_omits_applicant(c, named) and not c.get("date_of_death") for c in cases):
            obit_conflict = ("obituary ruled out: a common name, and it does not mention the person the court "
                             "named - a namesake's obituary")
            obit["owner_deceased"] = ""
    # birth year: DataSift's owner age vs the year the obituary says they were born
    born = owner_birth_year(rec)
    crm = f"{rec.get('owner_first') or ''} {rec.get('owner_last') or ''}".strip()
    obit_birth = ((verify.get("obit_birth") or {}).get(rec.get("uuid") or "") or {}).get("birth_year")
    obit_birth_ok = False
    if obit.get("owner_deceased") == "yes" and born and obit_birth and crm:
        who = obit_person(rec, cd, obit)
        if name_match(who, crm) != "no" or _same_person(crm, who):
            if abs(obit_birth - born) > 3:
                obit_conflict = (f"obituary ruled out: it gives a birth year of {obit_birth}, but the owner "
                                 f"{crm} was born about {born} (DataSift age) - a different person")
                obit["owner_deceased"] = ""
            elif abs(obit_birth - born) <= 2:
                obit_birth_ok = True
    dod = obit.get("date_of_death") or "" if obit.get("owner_deceased") == "yes" else ""
    obit_yes = obit.get("owner_deceased") == "yes"
    if obit_yes:
        death_pts += 30
        reasons.append(f"obituary match{(' (DOD ' + dod + ')') if dod else ''}"
                       + (f", birth year {obit_birth} matches the owner's age" if obit_birth_ok else ""))
    memorials, possible_memorials = memorial_deaths(rec, cd, deeds, verify)
    anc_deaths, census_leads = ancestry_deaths(rec, cd, deeds, verify)
    contradicted = []

    def _contradicts(m: dict) -> str:
        died, who = m.get("death_date") or "", m.get("person") or ""
        if not died[:4].isdigit():
            return ""
        estate_by = _recorded_death(who, deeds)
        if estate_by and int(died[:4]) - int(estate_by[:4]) > 2:
            return f"their estate was on a deed by {estate_by}"
        court = next((c.get("date_of_death") for c in cases if c.get("date_of_death")
                      and name_match(_case_decedent(c), who) != "no"), "")
        if court and abs(int(court[:4]) - int(died[:4])) > 1:
            return f"the court says they died {court}"
        if dod and obit_yes and name_match(obit_person(rec, cd, obit), who) != "no" \
                and abs(int(dod[:4]) - int(died[:4])) > 1:
            return f"the obituary says they died {dod}"
        return ""
    for src in (memorials, anc_deaths):
        for m in list(src):
            why = _contradicts(m)
            if why:
                src.remove(m)
                contradicted.append(f"{m.get('source') or 'Find A Grave'} for {m.get('person')} (d. "
                                    f"{m.get('death_date')}) set aside: {why}")
    # Only a memorial's name says they died, and someone pays the taxes under that name afterward: a person
    # paying their own taxes is alive. Daniel Alvarado paid 2026-10-14; the 2025 memorial is a namesake.
    # (After an obituary the same payment is the ghost-payer signal, so obituaries are read by a person.)
    recorded = bool(cases) or bool(ESTATE_RE.search(owner_raw)) or any(DEATH_DEED_RE.search(d.get("type") or "") for d in deeds)
    if not recorded and not obit_yes:
        for p in pays:
            p["class"] = classify_payer(p.get("payer", ""), [owner_raw] if owner_raw else [],
                                        [d.get("grantee") or "" for d in deeds[1:]] + [d.get("grantor") or "" for d in deeds])
        for src in (memorials, anc_deaths):
            for m in list(src):
                paid = [g for g in ghost_payments(pays, [{"name": m["person"], "dead_by": m.get("death_date") or ""}],
                                                  rec["county"]) if g["strength"].startswith("strong") and m.get("death_date")]
                if paid:
                    src.remove(m)
                    contradicted.append(f"{m.get('source') or 'Find A Grave'} for {m.get('person')} (d. {m.get('death_date')}) "
                                        f"set aside: {paid[0]['payer']} paid the taxes {paid[0]['date']}, after that date")
    if memorials:
        m = memorials[0]
        death_pts += 20
        reasons.append(f"Find A Grave: {m['name']} {m.get('birth_year') or '?'}-{m.get('death_year') or '?'} "
                       f"({m['why']})")
        dod = dod or (m["death_date"] if not m["death_date"].endswith("-12-31") else "")
    if anc_deaths:
        x = anc_deaths[0]
        death_pts += 25 if x["index"] else 15
        reasons.append(f"{x['source']}: {x.get('name')} d. {x['death_year']}"
                       + (f", b. {x['birth_year']}" if x.get("birth_year") else ""))
    est_label = bool(ESTATE_RE.search(owner_raw)) or bool(ESTATE_RE.search(rec.get("owner_company") or ""))
    if est_label:
        death_pts += 25
        reasons.append("county roll lists the owner as an ESTATE")
    if LIFE_ESTATE_RE.search(owner_raw):
        reasons.append("LIFE ESTATE on the roll (not a death) - if the life tenant has died, the "
                       "remaindermen own it and must sign")
    case = next((c for c in cases if c.get("case_number")), None)
    if case:
        death_pts += 20
        reasons.append(f"probate case {case['case_number']}")
        dod = dod or case.get("date_of_death") or ""
    vested = vesting_date(deeds, owner_raw)
    death_deeds = [d for d in deeds if DEATH_DEED_RE.search(d.get("type") or "") and (d.get("date") or "") >= vested]
    if not death_deeds and deeds and ESTATE_RE.search(deeds[0].get("grantee") or "") and not ESTATE_RE.search(owner_raw):
        death_deeds = [deeds[0]]   # the newest deed conveys INTO an estate: recorded proof, whatever its type says
    survivor = survivor_holds_title(deeds, owner_raw)
    if survivor:
        # 'LYNN ALVIN & MARY' -> death certificate -> 'LYNN MARY': Alvin's death already passed his
        # share to Mary, who owns it all and is alive - the spouse-death trap, not a title problem
        reasons.append(f"a death on this title is already settled: {survivor['grantee'].strip()} took title by "
                       f"{survivor['type'].lower()} {survivor['date']} (already cured)")
        death_deeds = []
    if death_deeds:
        death_pts += 15
        reasons.append(f"{(death_deeds[0].get('type') or 'deed to the estate').lower()} recorded {death_deeds[0]['date']}")
    if rec.get("owner_deceased"):
        death_pts += 10
        reasons.append("DataSift owner flagged deceased")
    if "list:Pre-Probate/Deceased" in rec.get("signals", []):
        death_pts += 10
        reasons.append("on Pre-Probate/Deceased list")
    death_pts = min(death_pts, 45)

    # ── title state ──
    owner_entity = is_entity(owner_raw)
    last = deeds[0] if deeds else {}
    title = "unknown"
    if cd.get("confidential"):
        title = "confidential_owner"
    elif owner_entity and not est_label:
        title = "entity_owner"
        drops.append(f"current owner is an entity ({owner_raw})")
    elif last and is_entity(last.get("grantee") or "") and not est_label:
        title = "sold_to_entity"
        drops.append(f"deeded to {last['grantee']} {last['date']}")
    elif last and last.get("grantee") and owner_raw and not _shares_name(last["grantee"], owner_raw)             and _shares_name(last.get("grantor") or "", owner_raw)             and not _shares_name(last["grantee"], last.get("grantor") or ""):
        title = "conveyed_out"
        drops.append(f"deeded out to {last['grantee']} {last['date']} - the tax roll still shows the old owner")
    elif survivor:
        title = "survivor_owns"
    elif death_deeds and death_deeds[0] is last:
        title = "heirship_recorded"
        reasons.append("title moved to heirs (partly cured)")
    elif dod and last and (last.get("date") or "") < dod:
        title = "stuck_in_decedent"
    elif not last or (_years_between(last.get("date") or today, today) or 0) >= 10:
        title = "no_transfer_10y"
    else:
        title = "recent_transfer"

    # ── who pays ──
    owner_groups = [owner_raw] if owner_raw else []
    prior_groups = [d.get("grantee") or "" for d in deeds[1:]] + [d.get("grantor") or "" for d in deeds]
    classes = []
    for p in pays:
        p["class"] = classify_payer(p.get("payer", ""), owner_groups, prior_groups)
        p["after_dod"] = bool(dod and (p.get("date") or "") > dod)
        classes.append(p["class"])
    recent = [p for p in pays if (p.get("date") or "") >= f"{date.today().year - 1}-{date.today():%m-%d}"]
    pay_pts = 0
    non_owner = [p for p in pays if p["class"] in ("same_surname_relative", "other_individual", "prior_owner")]
    if any(p["class"] == "title_company" for p in recent):
        drops.append("a title company paid the taxes in the last 12 months (closing / refinance)")
    if non_owner:
        pay_pts += 15
        who = sorted({p["payer"] for p in non_owner})[:3]
        reasons.append("taxes paid by someone other than the owner: " + "; ".join(who))
    dead = dead_people(rec, cd, obit, cases, deeds, memorials + anc_deaths)
    if survivor:   # the co-owner who died off title: their name on the tax account is just habit
        dead = [d for d in dead if any(name_match(n, d["name"]) != "no" for n in person_names(owner_raw))]
    if vested:     # died before this owner bought: the title moved on (Yates 2025, Mackey 2022)
        dead = [d for d in dead if on_roll(d["name"], owner_raw) or not d["dead_by"] or d["dead_by"] >= vested]
    if not dod:
        dod = min((p["dead_by"] for p in dead if p["dead_by"]), default="")
        for p in pays:
            p["after_dod"] = bool(dod and (p.get("date") or "") > dod)
    living = [n for n in county_people(rec, cd, deeds)
              if not any(name_match(n, d["name"]) != "no" or _same_person(d["name"], n) for d in dead)]
    ghosts = ghost_payments(pays, dead, rec["county"], living)
    timed = [g for g in ghosts if g["timing_known"]]
    strong = [g for g in timed if not g["strength"].startswith("weak")]
    if strong:
        pay_pts += 25
        reasons.append(f"GHOST PAYER: {len(strong)} payment(s) in {strong[0]['dead_person']}'s name AFTER they "
                       f"were dead (by {strong[0]['dead_by']}); last {strong[0]['date']} - someone is paying under them")
    elif timed:
        pay_pts += 10
        reasons.append(f"payments posted under {timed[0]['dead_person']}'s account after death "
                       f"(last {timed[0]['date']}; {timed[0]['strength']})")
    elif ghosts:
        reasons.append(f"payments in {ghosts[0]['dead_person']}'s name, date of death unknown - check timing")
    last_pay = pays[0]["date"] if pays else ""
    due_now = cd.get("total_due") or 0
    two_years = f"{date.today().year - 2}-{date.today():%m-%d}"
    struggling = bool(due_now) and any((p.get("date") or "") >= two_years for p in pays)
    if struggling:
        pay_pts += 10 if strong else 5
        reasons.append(f"still paying but cannot catch up (${due_now:,.0f} still due, last payment {last_pay})")
    defects = chain_defects(deeds, dead, owner_raw)
    court_reasons, court_defects, court_notes = grade_court_cases(rec, cd, deeds, other_cases or [])
    reasons += court_reasons
    defects = list(dict.fromkeys(defects + court_defects))
    defect_pts = min(len(defects) * 8, 20)
    if last_pay and (_years_between(last_pay, today) or 0) >= 1.5:
        pay_pts += 10
        reasons.append(f"payments stopped {last_pay}")
    if any(p["class"] == "tax_lender" for p in pays):
        reasons.append("property-tax lender involved")

    # ── money ──
    due = _money(cd.get("total_due") or rec.get("tax_delinquent_value") or 0) or 0
    # value = the tax assessor (CAD) market value only (owner decision 2026-10-29)
    mv = _money(cd.get("market_value") or (rec.get("value") or {}).get("market") or 0) or 0
    legal = ((verify.get("legal") or {}).get(f"{rec.get('county')}|{rec.get('apn')}") or {}).get("legal") or ""
    died_on_roll = min((p["dead_by"] for p in dead if p["dead_by"] and on_roll(p["name"], owner_raw)), default="")
    clerk_open, clerk_notes = clerk_open_for(rec, deeds, legal, died_on_roll)
    arr = arrears(rec, {**(rec.get("value") or {}), "taxes_due": due, "source": cd.get("source") or ""}, clerk_open)
    dq = delinquency(rec, cd)
    yrs = dq["years"]
    if not dq["source"]:   # county roll unavailable: fall back to the CRM's count
        try:
            yrs = int(float(rec.get("year_behind_on_taxes")
                            or (rec.get("custom_fields") or {}).get("Years Delinquent") or 0))
        except (TypeError, ValueError):
            yrs = 0
    money_pts = 0
    if dq["source"].startswith(("not on", "no delinquent")):
        reasons.append(f"{dq['source']} - paid up or current-year only, so no tax pressure right now")
    if 2 <= yrs <= 4:
        money_pts += 10
        reasons.append(f"{yrs} years behind on taxes (2-4 = the title-problem sweet spot)")
    elif yrs >= 5:
        money_pts += 5
        reasons.append(f"{yrs} years behind - long delinquencies are often heir-occupied; check who lives there")
    spread = round(mv * AS_IS_FACTOR - arr["known_total"]) if mv else None
    if spread is not None and spread >= 2 * GATE_MIN:
        money_pts += 5
    title_pts = {"stuck_in_decedent": 15, "no_transfer_10y": 10, "heirship_recorded": 5,
                 "confidential_owner": 0, "unknown": 5, "recent_transfer": 0}.get(title, 0)

    score = 0 if drops else death_pts + pay_pts + title_pts + money_pts + defect_pts
    if drops:
        tier = "Drop"
    elif death_pts < 15:
        tier = "C"   # nothing confirms a death yet: research, don't market as curative
    elif score >= 60:
        tier = "A"
    elif score >= 40:
        tier = "B"
    else:
        tier = "C"
    if tier == "A" and dq["source"].startswith(("not on", "no delinquent")):
        tier = "B"   # no tax pressure: still a title problem, but nothing forces a sale
        reasons.append("capped at B: not delinquent on the county roll")
    proof = death_proof(cases, death_deeds, est_label, memorials, obit_yes, any(x["index"] for x in anc_deaths))
    # ── gates ──
    problem = tier != "Drop" and (death_pts >= 15 or bool(defects) or bool(ghosts))
    if spread is None:
        gate = "1 - HOLD: no CAD value"
    elif spread < GATE_MIN:
        gate = "1 - FAIL: arrears eat the equity"
    elif not problem:
        gate = "2 - FAIL: no curative problem found"
    elif any(n.startswith("clerk records not searched") for n in clerk_notes):
        gate = "2 - PENDING: clerk liens not searched yet"
    else:
        gate = "3 - PASSED GATE 2 -> deep research: heirs, skip trace, call"
    if gate.startswith("1") and tier in ("A", "B"):
        tier = "C"
        reasons.append(f"spread ${spread:,.0f} under the ${GATE_MIN:,.0f} gate after all known arrears"
                       if spread is not None else "no CAD market value")
    heirs = []
    try:
        heirs = real_heirs(json.loads(obit.get("heir_map_json") or "[]"), obit_person(rec, cd, obit)) if obit_yes else []
    except (TypeError, ValueError):
        pass
    return {"tier": tier, "score": score, "death_pts": death_pts, "title": title, "dod": dod,
            "owner_raw": owner_raw, "reasons": reasons, "drops": drops, "payments": pays,
            "payer_classes": sorted(set(classes)), "last_payment": last_pay, "total_due": due,
            "market_value": mv, "years_delinquent": yrs, "deeds": deeds, "probate": cases,
            "heirs": heirs if isinstance(heirs, list) else [], "dead": dead, "ghosts": ghosts,
            "struggling": struggling, "defects": defects, "possible_probate": possible_cases,
            "ruled_out": ruled_out + [x for x in (obit_conflict, household_death) if x] + contradicted,
            "legal": legal, "owner_dead": any(on_roll(p["name"], owner_raw) for p in dead),
            "obituary_url": obit.get("obituary_url") or "",
            "first_delinquent_year": dq["first_year"], "years_source": dq["source"], "spread": spread,
            "death_proof": proof, "memorials": memorials, "possible_memorials": possible_memorials,
            "court_notes": court_notes, "ancestry_deaths": anc_deaths, "census_leads": census_leads,
            "arrears": arr, "clerk_open": clerk_open, "clerk_notes": clerk_notes, "gate": gate,
            "problem": problem}


# ── deal brief: the story, the six defects, the edge, the offer (Ryan / Logan Fullmer boot camp day 4) ──
# Source: "Curative Title Playbook/DEAL_STRUCTURES_AND_DEFECTS.md". New training transcripts get merged
# there first, then into these tables - keep the two in step.

DEFECT_FIX = {
    "Orphaned estate": "Affidavit of Heirship (heir affiant + 2 disinterested witnesses, recorded); with a will "
                       "and under 4 yrs since death, muniment of title (cheap, ~60 days). Trust but verify the "
                       "family tree: a predeceased child's kids inherit that share",
    "Unreleased lien / judgment": "Check age + name first (expired? same person? else a not-same-person affidavit); "
                                  "negotiate a partial release at 10-25 cents on the dollar or buy the debt and "
                                  "release it; never tell a creditor a sale is pending",
    "Missing owner": "Search ladder: skip trace, Ancestry, vital records, newspapers.com, relatives/neighbors/"
                     "social, then an investigator - make the business decision after ~4 days",
    "Feuding owners": "Buy the cooperative shares first; partition (in kind / by sale / by appraisal) only once you "
                      "own a big share (50%+), and never against an owner who lives there",
    "Erroneous instrument": "Correction affidavit for a small error; re-issued deed for a wrong legal; quiet title "
                            "if everyone involved is dead",
    "Break in chain": "Find and record the missing deed / divorce decree / name change; have the prior owner "
                      "re-issue it if too old; quiet title as the last resort",
}

OFFER_WHY = {
    "Net cash offer": "the auction clock is running - pay the arrears up front to stop the sale, seller nets a set "
                      "amount",
    "Subject to": "there is lead time - pay the seller now, carry the debts and pay them at resale "
                  "(smallest cash in)",
    "Joint venture": "a living owner with big equity who won't take a low cash number - you pay the taxes, list it "
                     "together, keep ~35% after costs (more if you cure title); you never take ownership risk",
    "Installment sale": "someone lives there - half at closing, half at move-out after the walk-through "
                        "(funds the eviction/cleanup if they don't leave)",
    "Seller finance": "an older owner whose benefits a lump sum would hurt - small down, monthly payments while "
                      "a long title fix plays out",
    "Heir buyouts (deed + promissory note)": "several heirs - buy each share with a recorded deed, small cash up "
                                             "front and the balance at resale; JV with the last holdout",
    "Gross offer": "a clean deal or a seller with an attorney - one price, title company pays the debts, title "
                   "policy",
}


def _occupied(rec: dict) -> bool | None:
    m = rec.get("owner_mailing") or {}
    if not m.get("street"):
        return None
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", (s or "").upper())[:12]   # noqa: E731
    return norm(m.get("street")) == norm(rec.get("street"))


def six_defects(rec: dict, a: dict) -> dict[str, list[str]]:
    """Map everything the audit found onto Ryan's six common title defects -> {defect: [evidence]}."""
    out: dict[str, list[str]] = {}
    add = lambda k, v: out.setdefault(k, []).append(v)   # noqa: E731
    probate_done = any(re.search(r"CLOSED|DISPOSED|INVENTORY APPROVED", c.get("type_status") or "", re.I)
                       for c in a.get("probate") or [])
    if a.get("dead") and a.get("title") in ("stuck_in_decedent", "no_transfer_10y", "unknown") and not probate_done:
        p = a["dead"][0]
        add("Orphaned estate", f"{p['name']} died by {p['dead_by'] or '?'} and the deed is still in their name"
            + ("" if a.get("probate") else " - no probate found"))
    for c in a.get("probate") or []:
        st = c.get("type_status") or ""
        if not re.search(r"CLOSED|DISPOSED", st, re.I):
            add("Orphaned estate", f"estate case {c['case_number']} still open ({st})"
                + (f", {c.get('pr_role') or 'PR'} {c['pr_name']}" if c.get("pr_name") else ""))
        if re.search(r"CONTEST|COUNTER|OBJECTION", " ".join(c.get("events") or []), re.I):
            add("Feuding owners", f"estate case {c['case_number']} is contested (counter-claim/contest filed)")
    for d in a.get("defects") or []:
        if re.match(r"(HEIRS HOLD SHARES|TOD NOT COMPLETED|DEED TO A DEAD PERSON)", d):
            add("Orphaned estate", d)
        elif re.match(r"(CHAIN BREAK|OWNER NOT IN THE DEED HISTORY|DEAD GRANTOR)", d):
            add("Break in chain", d)
        elif re.match(r"(FRACTIONAL OWNERS|DIVORCE BETWEEN CO-OWNERS|TITLE SUIT)", d):
            add("Feuding owners", d)
        else:
            add("Break in chain", d)
    for i in a.get("clerk_open") or []:
        add("Unreleased lien / judgment", f"{i.get('doc_type') or i.get('class')} {i.get('date') or ''} "
                                          f"({i.get('creditor') or '?'}) #{i.get('instrument') or '?'}")
    for n in a.get("clerk_notes") or []:
        if n.startswith("aged out"):
            add("Unreleased lien / judgment", n)
    m = rec.get("owner_mailing") or {}
    if m.get("state") and m["state"].upper() not in ("TX", "TEXAS"):
        add("Missing owner", f"owner mails to {m.get('city') or '?'}, {m['state']} - out of state")
    if any(re.search(r"UNSERVED", n, re.I) for n in a.get("court_notes") or []):
        add("Missing owner", "court papers came back unserved - the county can't find them either")
    return out


def pick_offer(rec: dict, a: dict, defects: dict) -> list[str]:
    """Ranked offer structures for this fact pattern (first = lead with it)."""
    picks: list[str] = []
    auction = any("OPEN TAX SUIT" in r for r in a.get("reasons") or []) or \
        any("foreclos" in w.lower() for w in (a.get("arrears") or {}).get("warnings") or [])
    heirs = "Orphaned estate" in defects or len(a.get("dead") or []) and len(a.get("heirs") or []) > 1
    living = not a.get("dead")
    big = (a.get("spread") or 0) >= 2 * GATE_MIN
    occupied = _occupied(rec)
    born = owner_birth_year(rec)
    if auction:
        picks.append("Net cash offer")
    if heirs:
        picks.append("Heir buyouts (deed + promissory note)")
    if living and big and not defects:
        picks.append("Joint venture")
    if living and big and a.get("struggling"):
        picks.append("Joint venture")
    if occupied:
        picks.append("Installment sale")
    if living and born and date.today().year - born >= 65:
        picks.append("Seller finance")
    if not auction:
        picks.append("Subject to")
    if not defects:
        picks.append("Gross offer")
    return list(dict.fromkeys(picks))[:3]


def deal_brief(rec: dict, a: dict, obit: dict, top_rank: int | None = None) -> list[str]:
    """The fact-pattern story, the defects, the edge and the offer - the top of every record's Notes."""
    L: list[str] = []
    if top_rank:
        L.append(f"*** TOP 100 #{top_rank} - skip trace everyone on title + heirs, deep prospect, call ***")
    deeds = a.get("deeds") or []
    owner = a.get("owner_raw") or "the owner of record"
    took = next((d for d in reversed(deeds) if d.get("grantee") and _shares_name(d["grantee"], owner)), None)
    story = [f"{owner} " + (f"took title {took['date']} by {(took.get('type') or 'deed').lower()} from "
                             f"{took.get('grantor') or '?'}." if took else
                             "- no deed in the history puts title in them (check for a break in chain)." if deeds else
                             "- deed history is not online for this parcel; pull it at the clerk.")]
    occ = _occupied(rec)
    if occ is not None:
        story.append("They mail to the property (likely owner-occupied)." if occ else
                     f"Mail goes elsewhere ({(rec.get('owner_mailing') or {}).get('city') or '?'}) - likely "
                     "vacant, rented or heir-occupied.")
    for p in (a.get("dead") or [])[:2]:
        story.append(f"{p['name']} died by {p['dead_by'] or 'an unknown date'} ({a.get('death_proof') or 'unproven'}).")
    for c in (a.get("probate") or [])[:1]:
        story.append(f"Estate case {c['case_number']} ({c.get('type_status')}"
                     + (f", filed {c['filed']}" if c.get("filed") else "") + ").")
    for c in (a.get("possible_probate") or [])[:2]:
        story.append(f"Possible estate case {c['case_number']} {c.get('style', '')} ({c.get('type_status')}"
                     + (f", filed {c['filed']}" if c.get("filed") else "") + ") - name-only match, verify it is "
                     "the owner before treating them as dead.")
    if a.get("ghosts"):
        g = a["ghosts"][0]
        story.append(f"Someone kept paying taxes in {g['dead_person']}'s name after death ({g['date']}) - "
                     "find who; they are usually the heir in control.")
    elif a.get("struggling"):
        story.append(f"They are still paying but can't catch up (last payment {a.get('last_payment')}).")
    elif a.get("last_payment"):
        story.append(f"Last tax payment {a['last_payment']}.")
    if a.get("years_delinquent"):
        story.append(f"{a['years_delinquent']} year(s) behind, ${a.get('total_due') or 0:,.0f} owed.")
    for n in (a.get("court_notes") or [])[:2]:
        story.append(n + ".")
    arr = a.get("arrears") or {}
    if a.get("market_value"):
        story.append(f"CAD value ${a['market_value']:,.0f}; known arrears ${arr.get('known_total', 0):,.0f}"
                     + (f" + {len(arr.get('unknown') or [])} of unknown amount" if arr.get("unknown") else "")
                     + f"; spread ${a.get('spread') or 0:,.0f}.")
    L.append("STORY: " + " ".join(story))

    defects = six_defects(rec, a)
    if defects:
        L.append("TITLE DEFECTS (of the six):")
        for k, ev in defects.items():
            L.append(f"  {k}: " + "; ".join(dict.fromkeys(ev))[:600])
    else:
        L.append("TITLE DEFECTS: none found in the records searched - a clean-title deal, so price it like one")

    edge = []
    if defects:
        edge.append("title/heir problem retail buyers, agents and title companies won't touch - you are buying "
                    "the problem at a discount and selling the cure")
    if any("OPEN TAX SUIT" in r for r in a.get("reasons") or []):
        edge.append("the county is already suing - a real deadline, and every other buyer waits for the auction")
    if a.get("dead") and not a.get("probate"):
        died = (a["dead"][0].get("dead_by") or "")[:4]
        if died.isdigit() and date.today().year - int(died) >= 4:
            edge.append("4+ years since death: a regular probate is likely barred, so heirship affidavit "
                        "buyouts are the path - few investors know it")
        else:
            edge.append("no probate opened - the family hasn't acted; you bring the plan")
    if a.get("ghosts"):
        edge.append("a ghost payer is your warm lead - one call reaches the person holding it together")
    if (a.get("spread") or 0) >= 2 * GATE_MIN:
        edge.append(f"deep equity (${a['spread']:,.0f} spread) leaves room to pay heirs fairly and still profit")
    if any("free and clear" in (i.get("label") or "") for i in arr.get("items") or []):
        edge.append("no mortgage on DataSift's estimate")
    if edge:
        L.append("MY EDGE: " + "; ".join(edge))

    offers = pick_offer(rec, a, defects)
    if offers:
        L.append("OFFER: " + offers[0] + " - " + OFFER_WHY[offers[0]])
        for o in offers[1:]:
            L.append(f"  or {o} - {OFFER_WHY[o]}")
    for k in defects:
        L.append(f"FIX ({k}): {DEFECT_FIX[k]}")
    L.append("BEFORE MONEY MOVES: option contract while you clear title; get eyes inside the house; "
             "re-run the clerk lien search; walk if the cure eats the spread")
    return L


def notes_block(rec: dict, a: dict, obit: dict, top_rank: int | None = None) -> str:
    L = [f"CURATIVE AUDIT {date.today():%Y-%m-%d} - tier {a['tier']} (score {a['score']})",
         f"Gate {a['gate']}"]
    L += deal_brief(rec, a, obit, top_rank)
    L.append("─── RESEARCH DETAIL ───")
    import curative_fields as cf  # noqa: PLC0415
    for label, url in cf.links(rec, a).items():
        L.append(f"{label}: {url}")
    L.append(f"County owner of record: {a['owner_raw'] or '(not found)'}")
    arr = a.get("arrears") or {}
    if arr:
        parts = [f"{i['label']} ${i['amount']:,.0f}" for i in arr["items"] if i["amount"]]
        L.append("Arrears (everything owed against the property): " + ("; ".join(parts) or "none found")
                 + f" = ${arr['known_total']:,.0f} known")
        for i in arr["unknown"]:
            L.append(f"  + AMOUNT UNKNOWN: {i['label']}")
        for w in arr["warnings"]:
            L.append(w)
    for line in a.get("clerk_notes", []):
        L.append("Clerk: " + line)
    if a["drops"]:
        L.append("NOT A CURATIVE TARGET: " + "; ".join(a["drops"]))
    if a["reasons"]:
        L.append("Why: " + "; ".join(a["reasons"]))
    for p in a.get("dead", []):
        L.append(f"Deceased: {p['name']} - dead by {p['dead_by'] or 'date unknown'} ({'; '.join(p['sources'])})")
    if a.get("dead"):
        L.append(f"Death proof: {a.get('death_proof') or 'none'}"
                 + (" - an obituary/memorial is a reference; confirm with a death certificate, probate or "
                    "heirship affidavit before closing" if a.get("death_proof") in
                    ("obituary only", "memorial only", "obituary + memorial") else ""))
    for m in a.get("memorials", []):
        L.append(f"Find A Grave: {_FAG}/memorial/{m['id']} {m['name']} ({m.get('birth_year') or '?'}-"
                 f"{m.get('death_year') or '?'}, {m['place']}) - {m['why']}")
    for line in a.get("possible_memorials", []):
        L.append("Possible " + line)
    for x in a.get("ancestry_deaths", []):
        L.append(f"{x['source']}: {x.get('name')} d. {x['death_year']} {x.get('url') or ''}".rstrip())
    for line in a.get("census_leads", []):
        L.append(line)
    for d in a.get("defects", []):
        L.append("TITLE DEFECT: " + d)
    for g in a.get("ghosts", [])[:3]:
        L.append(f"GHOST PAYMENT: {g['date']} ${g.get('amount') or 0:,.2f} paid as {g['payer']} - "
                 f"{g['dead_person']} was dead by {g['dead_by']} [{g['strength']}]")
    if obit.get("obituary_url") and not any(r.startswith("obituary ruled out") for r in a.get("ruled_out", [])):
        L.append(f"Obituary: {obit['obituary_url']}" + (f" (DOD {a['dod']})" if a["dod"] else ""))
    for c in a["probate"]:
        bits = [c["case_number"], c.get("style", ""), c.get("type_status", "")]
        if c.get("pr_name"):
            bits.append(f"{c.get('pr_role') or 'PR'}: {c['pr_name']}")
        if c.get("attorney_name"):
            bits.append(f"attorney {c['attorney_name']} {c.get('attorney_phone') or ''}".strip())
        L.append("Probate: " + " | ".join(b for b in bits if b))
    for c in a.get("possible_probate", []):
        L.append(f"Possible probate (first+last name match only - verify it is the owner): {c['case_number']} "
                 f"{c.get('style', '')}" + (f" | PR: {c['pr_name']}" if c.get("pr_name") else ""))
    for line in a.get("court_notes", []):
        L.append(line)
    for r in a.get("ruled_out", []):
        L.append("Ruled out: " + r)
    if a["deeds"]:
        L.append("Deed history (newest first):")
        for d in a["deeds"][:5]:
            L.append(f"  {d['date']} {d.get('type') or 'deed'}: {d.get('grantor') or '?'} -> {d.get('grantee') or '?'}"
                     + (f" #{d['instrument']}" if d.get("instrument") else ""))
    named = [p for p in a["payments"] if p.get("payer") and not p["payer"].startswith("?")]
    if named:
        L.append("Who paid the taxes (newest first):")
        for p in named[:8]:
            flag = " <- AFTER DEATH" if p.get("after_dod") else ""
            L.append(f"  {p['date']} ${p['amount'] or 0:,.2f} paid by {p['payer']} [{p['class']}]{flag}")
    elif rec["county"] == "Bell":
        L.append("Tax payer names are not published online in Bell (open-records request needed).")
    if a["total_due"] or a["years_delinquent"]:
        L.append(f"Back taxes: ${a['total_due']:,.2f}; years delinquent: {a['years_delinquent'] or '?'}"
                 + (f" (since {a['first_delinquent_year']})" if a.get("first_delinquent_year") else ""))
    if a.get("spread") is not None:
        L.append(f"Spread: ${a['spread']:,.0f} = 60% of ${a['market_value']:,.0f} CAD value minus "
                 f"${(a.get('arrears') or {}).get('known_total', 0):,.0f} known arrears")
    born = owner_birth_year(rec)
    if a.get("dead") and born and date.today().year - born >= 55:
        L.append("Check: Medicaid Estate Recovery (HHSC MERP) can claim against the estate if the decedent "
                 "had Medicaid long-term care after 55 - ask the family")
    if a["heirs"]:
        L.append("Possible heirs from the obituary (NOT verified signers):")
        for h in a["heirs"][:8]:
            L.append(f"  {h.get('name', '?')} - {h.get('relationship', '?')}"
                     + (f" ({h.get('status')})" if h.get("status") else ""))
    L.append("Next: confirm heirs + who must sign; skip trace the heirs only on approval.")
    return "\n".join(L)


TOP_N = 100
_GATE_RANK = (("3", 0), ("2 - PENDING", 1))


def top_opportunities(rows: list, n: int = TOP_N) -> list:
    """The call-first short list: Gate 3 before clerk-pending Gate 2, A before B, then score and spread.
    Never C/Drop, and one row per owner of record (one call covers all their parcels)."""
    def gate_rank(a: dict) -> int:
        return next((r for p, r in _GATE_RANK if a["gate"].startswith(p)), 9)
    pool = [t for t in rows if t[1]["tier"] in ("A", "B") and gate_rank(t[1]) < 9
            and not is_entity(re.sub(r"(?<!REAL )\bE(ST(ATE)?)?\b.*$", "", t[1].get("owner_raw") or ""))]
    # tier first: a clerk-pending A outranks a finished B; the clerk gate breaks ties within a tier
    pool.sort(key=lambda t: (t[1]["tier"], gate_rank(t[1]), -t[1]["score"], -(t[1].get("spread") or 0)))
    seen, out = set(), []
    for t in pool:
        owner = re.sub(r"[^A-Z]", "", (t[1].get("owner_raw") or t[0]["street"]).upper())
        if owner in seen:
            continue
        seen.add(owner)
        out.append(t)
        if len(out) >= n:
            break
    return out


def stage_score(out_dir: Path | None = None) -> Path:
    import csv  # noqa: PLC0415
    cands = _load(CANDIDATES, {}) or {}
    obits = _load(CACHE_DIR / "obituary.json", {}) or {}
    verify = _load(CACHE_DIR / "verify.json", {}) or {}
    verify["ancestry"] = _load(CACHE_DIR / "ancestry.json", {}) or {}
    verify["legal"] = _load(CACHE_DIR / "legal.json", {}) or {}
    verify["obit_review"] = _load(DATA_DIR / "obit_review.json", {}) or {}
    stamp = f"{date.today():%Y-%m-%d}"
    out_dir = out_dir or ROOT / "output" / f"{OUT_PREFIX}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for u, rec in cands.items():
        cd = county_data(rec)
        if not cd:
            continue
        obit = obits.get(u) or {}
        a = assess(rec, cd, obit, probate_cases(rec, cd), verify, court_cases(rec, cd))
        if a["obituary_url"]:   # the reviewed page, not a search-engine redirect
            obit = {**obit, "obituary_url": a["obituary_url"]}
        a["notes"] = notes_block(rec, a, obit)
        rows.append((rec, a, obit))
    order = {"A": 0, "B": 1, "C": 2, "Drop": 3}
    rows.sort(key=lambda t: (order[t[1]["tier"]], -t[1]["score"]))
    top_rank = {}
    for i, (rec, a, obit) in enumerate(top_opportunities(rows, TOP_N), 1):
        top_rank[rec["uuid"]] = i
        a["notes"] = notes_block(rec, a, obit, top_rank=i)
    _save(out_dir / "curative_audit.json", [{"record": r, "assessment": a, "obituary": o} for r, a, o in rows])

    # Reviewer workbook
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
    wb = Workbook()
    ws = wb.active
    ws.title = "Ranked"
    hdr = ["Tier", "Score", "County", "Property", "City", "ZIP", "County owner of record", "DataSift owner",
           "Death evidence", "Date of death", "Title state", "Who paid (classes)", "Last payment", "Taxes due",
           "Market value", "Probate case", "Possible heirs", "Why / drop reason", "APN", "DataSift lists",
           "Deceased (dead by)", "Ghost payer", "Still paying, behind", "Title defects",
           "Years behind", "Delinquent since", "Spread (60% CAD - all known arrears)", "Death proof", "Gate",
           "Known arrears", "Arrears amount unknown", "Look out"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
    fills = {"A": "C6EFCE", "B": "FFEB9C", "C": "EDEDED", "Drop": "F4CCCC"}
    for rec, a, obit in rows:
        ws.append([a["tier"], a["score"], rec["county"], rec["street"], rec["city"], rec["zip5"], a["owner_raw"],
                   " ".join(filter(None, [rec["owner_first"], rec["owner_last"], rec["owner_company"]])),
                   a["death_pts"], a["dod"], a["title"], ", ".join(a["payer_classes"]), a["last_payment"],
                   a["total_due"], a["market_value"], ", ".join(c["case_number"] for c in a["probate"]),
                   "; ".join(f"{h.get('name')} ({h.get('relationship')})" for h in a["heirs"][:5]),
                   "; ".join(a["drops"] or a["reasons"]), rec["apn"], ", ".join(rec.get("tax_lists") or []),
                   "; ".join(f"{p['name']} ({p['dead_by'] or '?'})" for p in a["dead"]),
                   "; ".join(f"{g['date']} {g['payer']} [{g['strength'].split()[0]}]" for g in a["ghosts"][:3]),
                   "YES" if a["struggling"] else "", "\n".join(a["defects"]),
                   a["years_delinquent"] or None, a.get("first_delinquent_year"), a.get("spread"),
                   a.get("death_proof"), a["gate"], a["arrears"]["known_total"],
                   "\n".join(i["label"] for i in a["arrears"]["unknown"]), "\n".join(a["arrears"]["warnings"])])
        ws.cell(ws.max_row, 1).fill = PatternFill("solid", fgColor=fills[a["tier"]])
    for col, w in zip(("A B C D E F G H I J K L M N O P Q R S T U V W X Y Z AA AB AC AD AE AF".split()),
                      (6, 6, 10, 26, 12, 7, 30, 24, 8, 11, 18, 26, 11, 11, 11, 14, 40, 70, 12, 22, 28, 40, 10, 80,
                       8, 10, 14, 20, 30, 12, 50, 60)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws2 = wb.create_sheet("Payments")
    ws2.append(["Property", "County", "Date", "Tax year", "Amount", "Paid by", "Payer class", "After death", "Channel"])
    for rec, a, _ in rows:
        for p in a["payments"]:
            ws2.append([rec["street"], rec["county"], p.get("date"), p.get("tax_year"), p.get("amount"),
                        p.get("payer"), p.get("class"), "YES" if p.get("after_dod") else "", p.get("channel")])
    # Gate 1 - every tax record: value vs everything owed
    g1 = wb.create_sheet("Gate 1 - Equity", 0)
    g1.append(["Gate 1", "County", "Property", "City", "ZIP", "APN", "CAD value", "60% of CAD", "Back taxes",
               "Est. mortgage (DataSift)", "Known arrears", "Spread", "Reason", "Look out", "Deceased signal"])
    for c in g1[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
    for u, rec in cands.items():
        g = rec.get("gate1") or {}
        arr = g.get("arrears") or {}
        items = {i["kind"]: i for i in arr.get("items") or []}
        g1.append(["PASS" if g.get("pass") else "fail", rec.get("county"), rec.get("street"), rec.get("city"),
                   rec.get("zip5"), rec.get("apn"), g.get("value"), g.get("value60"),
                   (items.get("taxes") or {}).get("amount"), (items.get("mortgage") or {}).get("amount"),
                   arr.get("known_total"), g.get("spread"), g.get("reason"), "\n".join(arr.get("warnings") or []),
                   ", ".join(rec.get("signals") or [])])
        g1.cell(g1.max_row, 1).fill = PatternFill("solid", fgColor="C6EFCE" if g.get("pass") else "F4CCCC")
    for col, w in zip("ABCDEFGHIJKLMNO", (7, 10, 28, 12, 7, 12, 11, 11, 11, 13, 12, 11, 60, 60, 30)):
        g1.column_dimensions[col].width = w
    g1.freeze_panes = "A2"
    g1.auto_filter.ref = g1.dimensions
    ws3 = wb.create_sheet("Notes preview")
    ws3.append(["Tier", "Property", "Notes block"])
    for rec, a, _ in rows:
        ws3.append([a["tier"], rec["street"], a["notes"]])
        ws3.cell(ws3.max_row, 3).alignment = Alignment(wrap_text=True, vertical="top")
    ws3.column_dimensions["B"].width = 28
    ws3.column_dimensions["C"].width = 120
    # Top 100: the short list to skip trace and call first
    top = top_opportunities(rows, TOP_N)
    wt = wb.create_sheet("Top 100", 0)
    top_hdr = ["Rank", "Gate", "Tier", "Score", "County", "Property", "City", "ZIP", "Owner of record",
               "Death proof", "Spread", "Known arrears", "Amount unknown", "Look out", "Years behind", "Why",
               "DataSift UUID", "APN"]
    wt.append(top_hdr)
    for c in wt[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
    top_rows = []
    for i, (rec, a, _) in enumerate(top, 1):
        top_rows.append([i, a["gate"], a["tier"], a["score"], rec["county"], rec["street"], rec["city"], rec["zip5"],
                         a["owner_raw"], a["death_proof"], a["spread"], a["arrears"]["known_total"],
                         " | ".join(i_["label"] for i_ in a["arrears"]["unknown"]),
                         " | ".join(a["arrears"]["warnings"]), a["years_delinquent"] or None,
                         "; ".join(a["reasons"])[:500], rec["uuid"], rec["apn"]])
        wt.append(top_rows[-1])
        wt.cell(wt.max_row, 3).fill = PatternFill("solid", fgColor=fills[a["tier"]])
    for col, w in zip("ABCDEFGHIJKLMNOPQR", (5, 26, 5, 6, 10, 26, 12, 7, 30, 20, 11, 11, 40, 50, 7, 80, 38, 12)):
        wt.column_dimensions[col].width = w
    wt.freeze_panes = "A2"
    _gate_sheets(wb, cands, rows)
    with (out_dir / "Top100_Opportunities.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(top_hdr)
        w.writerows(top_rows)
    xlsx = out_dir / f"Curative_Audit_{stamp}.xlsx"
    bad = re.compile("[^\x09\x0A\x0D\x20-퟿-�]")   # court/portal text can carry control chars
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and bad.search(cell.value):
                    cell.value = bad.sub("", cell.value)
    wb.save(xlsx)

    # Wizard CSVs: Notes + tags onto each record's EXISTING tax list (A/B only).
    hdr_up = ["Property Street Address", "Property City", "Property State", "Property ZIP Code",
              "Owner First Name", "Owner Last Name", "Mailing Street Address", "Mailing City",
              "Mailing State", "Mailing ZIP Code", "Tags", "Notes", "APN"]
    fresh = OUT_PREFIX.endswith("FRESH")
    batch = "Curative Fresh Wk1" if fresh else "Curative Wk1"   # fresh rows CREATE records: their own batch tag
    by_list: dict[str, list] = {}
    for rec, a, _ in rows:
        is_top = rec["uuid"] in top_rank
        if not is_top and (a["tier"] not in ("A", "B") or not a["gate"].startswith("3")):
            continue   # Gate 3 A/B (equity after all arrears + a curative problem) and the Top 100 go to the CRM
        lst = next((x for x in TAX_LISTS if x in (rec.get("tax_lists") or [])), "Tax Delinquent")
        if rec.get("no_house_number"):
            lst = "NO_HOUSE_NUMBER_review"   # DataSift's address validator rejects these: work them by hand
        m = rec.get("owner_mailing") or {}
        by_list.setdefault(lst, []).append([
            rec["street"], rec["city"], "TX", rec["zip5"],
            rec["owner_first"] or "", rec["owner_last"] or rec["owner_company"] or "",
            m.get("street") or "", m.get("city") or "", m.get("state") or "", m.get("zip5") or "",
            f"{batch},curative_{a['tier']}" + (",curative_top100" if is_top else ""), a["notes"], rec["apn"]])
    for lst, rs in by_list.items():
        p = out_dir / (f"{batch.replace(' ', '_')}_UPLOAD_{lst.replace(' ', '_')}.csv" if "review" not in lst
                       else f"{batch.replace(' ', '_')}_{lst}.csv")
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(hdr_up)
            w.writerows(rs)
    # one list per gate
    with (out_dir / "Gate1_Equity_PASS.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["County", "Property", "City", "ZIP", "APN", "CAD value", "Known arrears", "Spread", "Look out"])
        for rec in cands.values():
            g = rec.get("gate1") or {}
            if g.get("pass"):
                w.writerow([rec["county"], rec["street"], rec["city"], rec["zip5"], rec["apn"], g.get("value"),
                            (g.get("arrears") or {}).get("known_total"), g.get("spread"),
                            " | ".join((g.get("arrears") or {}).get("warnings") or [])])
    for gate_no, fname in (("2", "Gate2_Curative_PENDING_clerk.csv"), ("3", "Gate2_PASS_Gate3_DeepResearch.csv")):
        with (out_dir / fname).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Tier", "Score", "County", "Property", "City", "Owner of record", "Spread", "Known arrears",
                        "Amount unknown", "Death proof", "Why"])
            for rec, a, _ in rows:
                if a["gate"].startswith(gate_no) and (gate_no == "3" or "PENDING" in a["gate"]):
                    w.writerow([a["tier"], a["score"], rec["county"], rec["street"], rec["city"], a["owner_raw"],
                                a["spread"], a["arrears"]["known_total"],
                                " | ".join(i["label"] for i in a["arrears"]["unknown"]), a["death_proof"],
                                "; ".join(a["reasons"])[:500]])
    gates = {}
    for _, a, _ in rows:
        gates[a["gate"]] = gates.get(a["gate"], 0) + 1
    logger.info("gate 1: %d of %d pass; research gates: %s", sum(1 for r in cands.values() if gate1_passed(r)),
                len(cands), gates)
    tiers = {t: sum(1 for _, a, _ in rows if a["tier"] == t) for t in order}
    logger.info("scored %d records: %s -> %s", len(rows), tiers, out_dir)
    return out_dir


def _gate_sheets(wb, cands: dict, rows: list) -> None:
    """'Funnel' (counts per gate, second tab) + 'Gate 2 - Curative' (every Gate 1 pass, its Gate 2 result)."""
    from openpyxl.styles import Font, PatternFill  # noqa: PLC0415
    head = PatternFill("solid", fgColor="1F3A5F")
    g1_pass = sum(1 for r in cands.values() if gate1_passed(r))
    by_gate: dict[str, int] = {}   # '1F', '1H', '2F', '2P', '3'
    for rec, a, _ in rows:
        if gate1_passed(rec):
            g = a["gate"]
            k = g[:1] + ("P" if "PENDING" in g else "F" if "FAIL" in g else "H" if "HOLD" in g else "")
            by_gate[k] = by_gate.get(k, 0) + 1
    tiers = {t: sum(1 for _, a, _ in rows if a["gate"].startswith("3") and a["tier"] == t) for t in ("A", "B", "C")}
    fs = wb.create_sheet("Funnel", 1)
    fs.append(["Step", "Records", "What it means"])
    for c in fs[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head
    fs.append(["Screened", len(cands), "every tax-delinquent record in this run"])
    fs.append(["Gate 1 FAIL", len(cands) - g1_pass, "under $75K of equity after the known arrears, or no CAD value"])
    fs.append(["Gate 1 PASS -> Gate 2", g1_pass, "60% of the CAD value minus everything owed is at least $75K"])
    fs.append(["   Gate 2 FAIL", by_gate.get("2F", 0), "no curative problem found (no death, chain defect or "
                                                         "ghost payer)"])
    fs.append(["   Gate 2 FAIL (liens)", by_gate.get("1F", 0) + by_gate.get("1H", 0),
               "the clerk liens / judgments pushed the equity under the gate"])
    fs.append(["   Gate 2 PENDING", by_gate.get("2P", 0), "curative problem found, clerk lien search not run yet"])
    fs.append(["Gate 2 PASS -> Gate 3", by_gate.get("3", 0),
               "curative problem + equity after the clerk liens: ready for deep research"])
    for t, n in tiers.items():
        fs.append([f"   tier {t}", n, {"A": "strongest - work first", "B": "real problem, less pressure",
                                        "C": "weak or unproven"}[t]])
    fs.append(["Top 100", min(TOP_N, sum(tiers.values())), "Gate 3 A/B, one per owner, no companies: skip trace "
                                                           "+ deep prospect these"])
    for col, w in zip("ABC", (24, 10, 80)):
        fs.column_dimensions[col].width = w
    g2 = wb.create_sheet("Gate 2 - Curative", 2)
    g2.append(["Gate 2 result", "Tier", "County", "Property", "City", "Owner of record", "Death proof", "Spread",
               "Why"])
    for c in g2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head
    fill = {"PASS": "C6EFCE", "PENDING": "FFF2CC", "FAIL": "F4CCCC"}
    for rec, a, _ in rows:
        if not gate1_passed(rec):
            continue
        res = ("PASS" if a["gate"].startswith("3") else "PENDING" if "PENDING" in a["gate"] else "FAIL")
        why = a["gate"].split(": ", 1)[-1] if res == "FAIL" and a["gate"].startswith("2") else             "liens / judgments ate the equity" if res == "FAIL" else ""
        g2.append([res, a["tier"], rec["county"], rec["street"], rec["city"], a["owner_raw"], a["death_proof"],
                   a["spread"], (why + "; " if why else "") + "; ".join(a["reasons"])[:400]])
        g2.cell(g2.max_row, 1).fill = PatternFill("solid", fgColor=fill[res])
    for col, w in zip("ABCDEFGHI", (13, 5, 10, 28, 12, 30, 20, 11, 90)):
        g2.column_dimensions[col].width = w
    g2.freeze_panes = "A2"
    g2.auto_filter.ref = g2.dimensions


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stage", choices=["pull", "gate1", "county", "probate", "docket", "obit", "verify", "clerk",
                                      "legal", "ancestry", "score", "all"])
    ap.add_argument("--set", choices=sorted(SETS), default="account",
                    help="account = the DataSift tax records; fresh = county rolls minus the account")
    ap.add_argument("--workers", type=int, default=3, help="docket: parallel browsers per county")
    ap.add_argument("--refresh", action="store_true", help="ignore caches for this stage")
    ap.add_argument("--only", help="county filter: Travis | Williamson | Bell")
    ap.add_argument("--limit", type=int, default=0, help="process at most N records")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    global CANDIDATES, OUT_PREFIX
    CANDIDATES, OUT_PREFIX = SETS[args.set]
    if args.set == "fresh" and args.stage == "pull":
        ap.error("the fresh set is built by: python src/curative_roll.py build")
    if args.stage in ("pull", "all") and args.set == "account":
        stage_pull(refresh=args.refresh)
    if args.stage in ("gate1", "all"):
        stage_gate1(limit=args.limit)
    if args.stage in ("county", "all"):
        stage_county(only=args.only, limit=args.limit, refresh=args.refresh)
    if args.stage in ("probate", "all"):
        stage_probate(refresh=args.refresh)
    if args.stage in ("docket", "all"):   # Travis + Bell Odyssey Portal (Travis: 2Captcha per search)
        stage_docket(only=args.only, workers=args.workers)
    if args.stage in ("obit", "all"):
        stage_obit(limit=args.limit, refresh=args.refresh)
    if args.stage in ("verify", "all"):
        stage_verify(refresh=args.refresh)
    if args.stage in ("clerk", "all"):   # headed browsers, home IP
        stage_clerk(limit=args.limit, only=args.only or "")
    if args.stage in ("legal", "all"):
        stage_legal()
    if args.stage == "ancestry":   # logged-in headed browser, daily page cap: never part of "all"
        stage_ancestry(limit=args.limit, refresh=args.refresh)
    if args.stage in ("score", "all"):
        stage_score()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())

"""Configuration for SiftStack — Texas REI operations platform.

Covers Travis, Bell, and Williamson counties in central Texas.
"""

import json
import logging
import os
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = PROJECT_ROOT / "last_run.json"
SEEN_IDS_FILE = PROJECT_ROOT / "seen_ids.json"
DROPBOX_STATE_FILE = PROJECT_ROOT / "dropbox_state.json"
PHOTO_STATE_FILE = PROJECT_ROOT / "photo_state.json"
# Travis tax-delinquent cross-run state (skill-ported cleaner + dropped/new/repeat diff)
TRAVIS_TEXDEL_STATE_DIR = PROJECT_ROOT / "data" / "travis_tax_state"
TRAVIS_TEXDEL_RAW_DIR = PROJECT_ROOT / "data" / "travis_tax_raw"

# ── Dropbox Watcher ────────────────────────────────────────────────────
DROPBOX_POLL_INTERVAL = int(os.getenv("DROPBOX_POLL_INTERVAL", "900"))  # seconds (default 15 min)
DROPBOX_ROOT_FOLDER = os.getenv("DROPBOX_ROOT_FOLDER", "")  # root folder path in Dropbox, e.g. "/TX County Data"
DROPBOX_STORAGE_WARN_PERCENT = 80  # warn when storage usage exceeds this %

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ── Credentials ────────────────────────────────────────────────────────
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")      # 2Captcha API key (for Odyssey portals)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude Haiku for LLM parsing
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")        # Smarty address standardization
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")
OPENWEBNINJA_API_KEY = os.getenv("OPENWEBNINJA_API_KEY", "")  # Zillow property enrichment
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")              # Serper.dev Google Search API
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")        # Firecrawl JS-rendered scraping
TRACERFY_API_KEY = os.getenv("TRACERFY_API_KEY", "")          # Tracerfy skip tracing
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")            # Trestle phone validation
DATASIFT_EMAIL = os.getenv("DATASIFT_EMAIL", "")              # DataSift.ai login
DATASIFT_PASSWORD = os.getenv("DATASIFT_PASSWORD", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")        # Slack/Discord webhook
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")       # Drive folder for DataSift CSV staging
GOOGLE_SERVICE_ACCOUNT_KEY = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY", "")  # base64-encoded service account JSON
ANCESTRY_EMAIL = os.getenv("ANCESTRY_EMAIL", "")              # Ancestry.com login
ANCESTRY_PASSWORD = os.getenv("ANCESTRY_PASSWORD", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")            # Dropbox OAuth2 app key
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")

# Open-records (Bell/Williamson code enforcement PIA requests) — requester identity
# placed on every Texas Public Information Act request. Required before any send.
OPEN_RECORDS_REQUESTER_NAME = os.getenv("OPEN_RECORDS_REQUESTER_NAME", "")
OPEN_RECORDS_REQUESTER_EMAIL = os.getenv("OPEN_RECORDS_REQUESTER_EMAIL", "")
OPEN_RECORDS_REQUESTER_PHONE = os.getenv("OPEN_RECORDS_REQUESTER_PHONE", "")
OPEN_RECORDS_FEE_CAP = os.getenv("OPEN_RECORDS_FEE_CAP", "25")  # $ threshold to pause for estimate

# Code-enforcement relevance filter: drop closed cases + keep only neglect/distress
# violation types (LLM-classified). Set false to ingest raw, unfiltered cases.
CODE_VIOLATION_NEGLECT_FILTER = os.getenv(
    "CODE_VIOLATION_NEGLECT_FILTER", "true").lower() in ("1", "true", "yes")

# ── LLM Backend ──────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "anthropic")           # "anthropic", "ollama", or "openrouter"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")  # Anthropic model name (default for all LLM calls)
# High-stakes obituary identity + heir/survivor extraction uses a stronger model.
# Getting the heir/decision-maker chain right is critical: a wrong heir map sends
# the whole deal down the wrong path, so this defaults to Sonnet rather than Haiku.
OBITUARY_LLM_MODEL = os.getenv("OBITUARY_LLM_MODEL", "claude-sonnet-4-6")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")        # Local Ollama model
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")       # OpenRouter API key
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Per-call token-usage DEBUG logging (the accumulator always runs; this only
# gates the noisy per-call log line). Set LLM_USAGE_LOG=0 to silence.
LLM_USAGE_LOG = os.getenv("LLM_USAGE_LOG", "1") not in ("0", "false", "False", "")

# USD pricing per 1M tokens: {model: (input_per_M, output_per_M)}.
# Used to convert measured token usage into the Slack "Run cost" figure.
# NOTE: verify these against current Anthropic/OpenRouter pricing — rates drift.
LLM_PRICING = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),   # Haiku 4.5
    "claude-haiku-4-5": (1.0, 5.0),
    # Heir/survivor extraction runs on Sonnet (OBITUARY_LLM_MODEL); price it
    # correctly so the run-cost report doesn't undercount it at the Haiku rate.
    "claude-sonnet-4-6": (3.0, 15.0),          # Sonnet 4.6
}
# Fallback rate (per 1M in, out) for any model not in LLM_PRICING.
LLM_PRICING_DEFAULT = (1.0, 5.0)

# ── Rate Limiting ──────────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.0  # seconds between requests
REQUEST_DELAY_MAX = 3.0
MAX_RETRIES = 3

# ── Per-Run Spending Caps (USD) ────────────────────────────────────────
# Hard limits to prevent API cost runaway. When hit, the relevant step stops
# making more calls and logs a warning. Override via env vars if needed.
MAX_TRACERFY_COST_USD = float(os.getenv("MAX_TRACERFY_COST_USD", "100.0"))
MAX_ZILLOW_COST_USD = float(os.getenv("MAX_ZILLOW_COST_USD", "100.0"))
TRACERFY_COST_PER_RECORD = 0.02  # batch endpoint pricing
ZILLOW_COST_PER_LOOKUP = 0.01    # OpenWeb Ninja pricing after free tier

# ── Image Processing ───────────────────────────────────────────────────
BLUR_THRESHOLD = int(os.getenv("BLUR_THRESHOLD", "100"))   # Laplacian variance; below = rejected as blurry
TESSERACT_PSM_PDF = 3    # fully automatic — best for PDF tax sale tables
TESSERACT_PSM_PHOTO = 4  # assume single column of variable-size text — best for terminal screen photos

# ── Notice Types ───────────────────────────────────────────────────────
# code_violation is scraped for Travis (Austin Code Socrata API); Bell/Williamson
# have no live source (open-records only) and warn harmlessly as unregistered.
NOTICE_TYPES = ["foreclosure", "tax_sale", "tax_delinquent", "probate", "code_violation", "lien"]

# ── Texas Counties ────────────────────────────────────────────────────
# Target counties for scraping. Each maps to its data sources.
TX_COUNTIES = ["Travis", "Bell", "Williamson"]

# County → Appraisal District portal URLs (for property data enrichment)
CAD_URLS: dict[str, str] = {
    "Travis": "https://traviscad.org/propertysearch/",
    "Bell": "https://esearch.bellcad.org/",
    "Williamson": "https://search.wcad.org/",
}

# County → Odyssey portal URLs (for probate court records)
ODYSSEY_URLS: dict[str, str] = {
    "Travis": "https://odysseyweb.traviscountytx.gov/Portal/",
    "Bell": "https://www.justice.bellcounty.texas.gov/PublicPortal/",
    "Williamson": "https://judicialrecords.wilco.org/",
}

# Travis County direct data sources
TRAVIS_TAX_DELINQUENT_CSV = "https://tax-office.traviscountytx.gov/voterdata/TaxDelqOpenData.csv"
# "Tax Current Year and Prior Year Delinquent" master file: ~500K rows, has
# owner + mailing address + parcel, but NO situs (property street). For
# probate decedents (individuals) the mailing address is almost always their
# home property — used as a fallback situs when the smaller delinquent CSV misses.
TRAVIS_TAX_CURRENT_CSV = "https://tax-office.traviscountytx.gov/voterdata/TaxCurOpenData.csv"
TRAVIS_TAX_CACHE_DIR = "data/travis_tax_cache"
TRAVIS_TAX_CACHE_TTL_HOURS = 24  # both CSVs are refreshed daily by the county
TRAVIS_TAX_SALES_URL = "https://tax-office.traviscountytx.gov/properties/foreclosed/upcoming-sales"
TRAVIS_CLERK_URL = "https://www.tccsearch.org/"

# Bell County data sources
BELL_FORECLOSURES_URL = "https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php"

# Bell tax delinquent (BellCAD data portal). Filenames carry rotating date stamps;
# scraper fetches the portal index page each run and regexes the latest XLSX.
# - Delinquent file: parcel-level "Delinquent Roll All Years" — primary scrape target.
# - Appraisal file: full-county owner+situs+parcel — used as master cross-reference
#   for probate/foreclosure address backfill (BellCAD has no live API).
BELLCAD_DATA_PORTAL_URL = "https://bellcad.org/data-portal/"
BELLCAD_DELINQUENT_ROLL_PATTERN = r"BellCAD_Delinquent_Roll_Condensed_\d{8}\.xlsx"
BELLCAD_APPRAISAL_ROLL_PATTERN = r"\d{4}_BellCAD_Appraisal_Data_Condensed_\d{8}\.xlsx"
BELL_TAX_CACHE_DIR = "data/bell_tax_cache"
BELL_TAX_CACHE_TTL_HOURS = 24

# Williamson County data sources
WILCO_TRUSTEE_SALES_URL = "https://apps.wilco.org/countyclerk/trustee_sales/"

# Williamson tax delinquent (Wilco TAC). Index page lists CivicPlus DocumentCenter
# files; URL date tokens rotate weekly so scraper resolves the live link each run.
# Master cross-reference uses the existing live WCAD SODA API in cad_lookup.py
# (no separate master download — the API is free, real-time, and full-county).
WILCO_TAX_ROLL_PAGE = "https://www.wilcotx.gov/761/Property-Tax-Roll-Information-Request"
WILCO_DELINQUENT_DOC_ID = "8553"   # Current Year and Prior Taxes Due (Excel)
WILCO_TAX_CACHE_DIR = "data/wilco_tax_cache"
WILCO_TAX_CACHE_TTL_HOURS = 24

# MVBA Law Firm — handles tax sales for Bell + Williamson
MVBA_TAX_SALES_URL = "https://mvbalaw.com/tax-sales/"

# ── Entity Detection ──────────────────────────────────────────────────
# Business entity patterns — shared across obituary_enricher, tax_enricher,
# and enrichment_pipeline for entity filtering.
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO\b|LTD|LP|L\.P|"
    r"PARTNERSHIP|ASSOCIATION|ASSOC|BANK|CREDIT UNION|CHURCH|MINISTRIES|"
    r"HOUSING|AUTHORITY|DEVELOPMENT|ENTERPRISES|PROPERTIES|INVESTMENTS|"
    r"GROUP|HOLDINGS|MANAGEMENT|SERVICES|FOUNDATION|ORGANIZATION)\b",
    re.IGNORECASE,
)

# Trust/estate patterns — personal trusts are NOT business entities
TRUST_NAME_RE = re.compile(
    r"^(?:THE\s+)?([\w]+(?:\s+[\w.]+)+?)\s+(?:REVOCABLE\s+)?(?:LIVING\s+)?TRUST\b",
    re.IGNORECASE,
)
ESTATE_OF_RE = re.compile(
    r"^(?:THE\s+)?ESTATE\s+OF\s+([\w]+(?:\s+[\w.]+)+?)(?:\s*,|\s*$)",
    re.IGNORECASE,
)

_config_logger = logging.getLogger(__name__)


# ── State File Utilities ─────────────────────────────────────────────


def save_state(path: Path, data: dict) -> None:
    """Write JSON state to disk atomically (write tmp → rename).

    Creates a .bak copy of the previous file before overwriting.
    """
    # Back up current file
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass  # Best-effort backup

    # Atomic write: tmp → rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _config_logger.warning("Failed to read %s: %s", candidate, e)
    return {}

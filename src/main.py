"""Entry point for SiftStack — Texas REI operations platform.

Runs as either:
  - Apify Actor (when APIFY_IS_AT_HOME is set — reads input from Actor.get_input())
  - Standalone CLI (python src/main.py daily --counties Travis --types foreclosure)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import config
from config import (
    LOG_DIR,
    NOTICE_TYPES,
    OUTPUT_DIR,
    TX_COUNTIES,
)
from data_formatter import deduplicate, write_csv, write_csv_by_type

logger = logging.getLogger(__name__)


# ── CLI incremental state ─────────────────────────────────────────────
# Mirrors the Apify KVS `last_run_date` + `seen_notice_ids` persistence
# into local JSON files, so CLI `daily` runs are incremental: the scraper
# only fetches records since last run, and records already-processed get
# filtered out before enrichment. Monotonic — nothing prunes seen.


def _load_cli_state() -> tuple[str | None, set[str]]:
    """Read last_run_date + seen_notice_ids from disk. Returns empty state if missing."""
    last_run = None
    seen: set[str] = set()
    if config.STATE_FILE.exists():
        try:
            last_run = json.loads(config.STATE_FILE.read_text()).get("last_run_date")
        except Exception as e:
            logging.warning("Could not read %s: %s", config.STATE_FILE, e)
    if config.SEEN_IDS_FILE.exists():
        try:
            seen = set(json.loads(config.SEEN_IDS_FILE.read_text()))
            logging.info("Loaded %d previously-seen notice IDs from %s",
                         len(seen), config.SEEN_IDS_FILE.name)
        except Exception as e:
            logging.warning("Could not read %s: %s", config.SEEN_IDS_FILE, e)
    return last_run, seen


def _save_cli_state(seen: set[str]) -> None:
    """Persist seen IDs + today's date. Atomic write via temp file."""
    today = datetime.now().strftime("%Y-%m-%d")
    config.STATE_FILE.write_text(json.dumps({"last_run_date": today}))
    tmp = config.SEEN_IDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(config.SEEN_IDS_FILE)
    logging.info("Saved CLI state: last_run=%s, %d seen_ids", today, len(seen))


def _notice_dedup_key(notice) -> str:
    """Build a stable cross-run dedup key for a notice.

    Prefers a URL-derived notice ID when available (court records with
    ?ID=... param), then falls back to parcel ID, then to a composite
    of notice_type|county|address|zip. Tax-delinquent records from the
    Travis bulk CSV all share a generic source_url, so the composite
    path is what actually identifies them.
    """
    from data_formatter import _notice_id_from_url
    url = getattr(notice, "source_url", "") or ""
    nid = _notice_id_from_url(url)
    if nid:
        return f"id:{nid}"
    parcel = (getattr(notice, "parcel_id", "") or "").strip()
    if parcel:
        return f"parcel:{parcel}"
    addr = (getattr(notice, "address", "") or "").strip().lower()
    ntype = (getattr(notice, "notice_type", "") or "").strip().lower()
    county = (getattr(notice, "county", "") or "").strip().lower()
    zipc = (getattr(notice, "zip", "") or "").strip()[:5]
    return f"addr:{ntype}|{county}|{addr}|{zipc}"


def _notice_dedup_key_from_row(row: dict) -> str:
    """Same logic as _notice_dedup_key, but reads from a CSV DictReader row.
    Used during the one-time seeding step and for stable comparison."""
    from data_formatter import _notice_id_from_url
    url = (row.get("source_url") or "").strip()
    nid = _notice_id_from_url(url)
    if nid:
        return f"id:{nid}"
    parcel = (row.get("parcel_id") or "").strip()
    if parcel:
        return f"parcel:{parcel}"
    addr = (row.get("address") or "").strip().lower()
    ntype = (row.get("notice_type") or "").strip().lower()
    county = (row.get("county") or "").strip().lower()
    zipc = (row.get("zip") or "").strip()[:5]
    return f"addr:{ntype}|{county}|{addr}|{zipc}"


# ── Shared helpers ────────────────────────────────────────────────────


def _filter_targets(
    counties: list[str] | None,
    types: list[str] | None,
) -> list[tuple[str, str]]:
    """Build list of (county, notice_type) pairs to scrape.

    Returns all combinations of TX_COUNTIES × NOTICE_TYPES, filtered by
    the --counties and --types CLI args.
    """
    target_counties = TX_COUNTIES
    target_types = NOTICE_TYPES

    if counties:
        county_set = {c.lower() for c in counties}
        target_counties = [c for c in TX_COUNTIES if c.lower() in county_set]

    if types:
        type_set = {t.lower() for t in types}
        target_types = [t for t in NOTICE_TYPES if t.lower() in type_set]

    return [(c, t) for c in target_counties for t in target_types]


# ── Preflight health checks ─────────────────────────────────────────


def _preflight_check(mode: str) -> list[str]:
    """Verify required API keys and service connectivity before running.

    Returns a list of failure descriptions. Empty list = all checks passed.
    """
    failures: list[str] = []

    # ── Credential checks (mode-dependent) ──────────────────────────
    scrape_modes = {"daily", "historical"}
    enrichment_modes = scrape_modes | {"pdf-import", "photo-import", "dropbox-watch", "csv-import"}
    datasift_modes = {"manage-presets", "manage-sold", "phone-validate"}

    # No login credentials needed — all TX sources are public

    if mode in enrichment_modes:
        # These are warnings, not blockers — pipeline degrades gracefully
        if not config.SMARTY_AUTH_ID or not config.SMARTY_AUTH_TOKEN:
            logger.warning("Preflight: SMARTY credentials missing — address standardization will be skipped")
        if not config.OPENWEBNINJA_API_KEY:
            logger.warning("Preflight: OPENWEBNINJA_API_KEY missing — Zillow enrichment will be skipped")
        if not config.ANTHROPIC_API_KEY:
            logger.warning("Preflight: ANTHROPIC_API_KEY missing — obituary search and LLM parsing will be skipped")

    if mode in datasift_modes:
        if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
            failures.append("DATASIFT_EMAIL / DATASIFT_PASSWORD not set (required for DataSift operations)")

    if mode == "dropbox-watch":
        if not config.DROPBOX_APP_KEY or not config.DROPBOX_APP_SECRET or not config.DROPBOX_REFRESH_TOKEN:
            failures.append("DROPBOX credentials incomplete (need APP_KEY, APP_SECRET, REFRESH_TOKEN)")

    if mode == "phone-validate":
        if not config.TRESTLE_API_KEY:
            failures.append("TRESTLE_API_KEY not set (required for phone validation)")

    return failures


# ── Apify Actor mode ─────────────────────────────────────────────────


async def actor_main() -> None:
    """Run as an Apify Actor — full automated pipeline.

    Scrape → Dedup → Enrich → Tracerfy (opt) → PDFs → DataSift CSVs →
    Drive upload → Slack Notification. Mirrors `_run_scrape_pipeline()`
    (the CLI path) feature-for-feature, reading toggles from Actor input.

    State persistence across runs:
      - `last_run_date` (KVS) — daily since_date window
      - `seen_notice_ids` (KVS) — dedup keys monotonic growth
      - `travis_texdel_state` (KVS) — Travis tax-delinquent APN baseline
        for the NEW/REPEAT/DROPPED cross-run diff
    """
    from apify import Actor
    from time import time as _time
    from scrapers import travis_texdel_state as _texdel_state
    from scrapers import tax_delinquent_state as _gtexdel_state

    # Bell + Williamson share the generic, county-parameterized texdel state
    # module. (county, KVS key) pairs — slugs match county.lower() cache keys
    # and the *_texdel_state naming Travis uses.
    _GENERIC_TEXDEL = (
        ("Bell", "bell_texdel_state"),
        ("Williamson", "williamson_texdel_state"),
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async with Actor:
        pipeline_start = _time()
        # Isolate this run's token meter (the Actor container is long-lived and
        # may handle multiple runs) so the Slack "Run cost" reflects only this run.
        import llm_client
        llm_client.reset_usage()
        actor_input = await Actor.get_input() or {}

        # ── Credentials → config + env (downstream modules read either) ──
        _cred_map = {
            "ANTHROPIC_API_KEY": actor_input.get("anthropic_api_key", ""),
            "SMARTY_AUTH_ID": actor_input.get("smarty_auth_id", ""),
            "SMARTY_AUTH_TOKEN": actor_input.get("smarty_auth_token", ""),
            "OPENWEBNINJA_API_KEY": actor_input.get("openwebninja_api_key", ""),
            "SERPER_API_KEY": actor_input.get("serper_api_key", ""),
            "FIRECRAWL_API_KEY": actor_input.get("firecrawl_api_key", ""),
            "TRACERFY_API_KEY": actor_input.get("tracerfy_api_key", ""),
            "DATASIFT_EMAIL": actor_input.get("datasift_email", ""),
            "DATASIFT_PASSWORD": actor_input.get("datasift_password", ""),
            "SLACK_WEBHOOK_URL": actor_input.get("slack_webhook_url", ""),
            "TRESTLE_API_KEY": actor_input.get("trestle_api_key", ""),
        }
        for key, val in _cred_map.items():
            if val:
                setattr(config, key, val)
                os.environ[key] = val

        # ── Scrape scope ────────────────────────────────────────────
        mode = actor_input.get("mode", "daily")
        counties = actor_input.get("counties") or None
        types = actor_input.get("types") or None
        since_date_override = (actor_input.get("since_date") or "").strip()
        drive_folder_id = actor_input.get("google_drive_folder_id", "")
        drive_key_b64 = actor_input.get("google_service_account_key", "")
        if drive_folder_id:
            config.GOOGLE_DRIVE_FOLDER_ID = drive_folder_id
        if drive_key_b64:
            config.GOOGLE_SERVICE_ACCOUNT_KEY = drive_key_b64

        # ── Scraper kwargs (tax-delinquent thresholds + skill cleaner knobs) ──
        min_years = int(actor_input.get("min_delinquent_years", 2) or 2)
        min_amount = float(actor_input.get("min_delinquent_amount", 3000.0) or 3000.0)
        skip_texdel_cleaner = bool(actor_input.get("skip_texdel_cleaner", False))
        max_notices = int(actor_input.get("max_notices", 0) or 0) or None
        scraper_kwargs = {"min_years": min_years, "min_amount": min_amount}
        if skip_texdel_cleaner:
            scraper_kwargs["skip_cleaner"] = True

        # ── Enrichment flags (mirror CLI) ──
        enable_obituary = bool(actor_input.get("enable_obituary", False))
        enable_ancestry = bool(actor_input.get("enable_ancestry", False))
        enable_tracerfy = bool(actor_input.get("enable_tracerfy", False))
        enable_trestle = bool(actor_input.get("enable_trestle", False))
        research_entities = bool(actor_input.get("research_entities", False))
        keep_government = bool(actor_input.get("keep_government_records", False))
        keep_listed = bool(actor_input.get("keep_listed", False))
        skip_smarty = bool(actor_input.get("skip_smarty", False))
        skip_zillow = bool(actor_input.get("skip_zillow", False))
        skip_tax = bool(actor_input.get("skip_tax", False))
        skip_geocode = bool(actor_input.get("skip_geocode", False))
        skip_zip_filter = bool(actor_input.get("skip_zip_filter", False))
        skip_condo_filter = bool(actor_input.get("skip_condo_filter", False))
        skip_heir_verification = bool(actor_input.get("skip_heir_verification", False))
        skip_dm_address = bool(actor_input.get("skip_dm_address", False))
        max_heir_depth = int(actor_input.get("max_heir_depth", 2) or 2)
        obituary_workers = int(actor_input.get("obituary_workers", 4) or 4)

        # Legacy buy-box toggles (kept for backwards compatibility)
        exclude_vacant = bool(actor_input.get("exclude_vacant", False))
        exclude_commercial = bool(actor_input.get("exclude_commercial", False))
        exclude_entities = bool(actor_input.get("exclude_entities", False))

        # ── Operational toggles ──
        do_notify_slack = bool(actor_input.get("notify_slack", True))
        rebuild_dedup_only = bool(actor_input.get("rebuild_dedup_only", False))
        no_reports = bool(actor_input.get("no_reports", False))
        use_proxy = bool(actor_input.get("use_residential_proxy", False))  # Default OFF on Free plan

        # ── DataSift auto-upload (RAWPIPE) toggles ──
        upload_datasift_api = bool(actor_input.get("upload_datasift_api", False))
        datasift_do_enrich = not bool(actor_input.get("datasift_no_enrich", False))
        datasift_do_skip_trace = not bool(actor_input.get("datasift_no_skip_trace", True))

        # ── Filter targets ──
        targets = _filter_targets(counties, types)
        if not targets:
            Actor.log.error("No scrape targets match the given counties/types filters")
            await Actor.fail(status_message="No matching scrape targets")
            return

        Actor.log.info(
            "Running %d scrape targets: %s",
            len(targets),
            ", ".join(f"{c}/{t}" for c, t in targets),
        )

        # ── Residential proxy (default OFF on Free plan) ──
        if use_proxy:
            try:
                proxy_config = await Actor.create_proxy_configuration(groups=["RESIDENTIAL"])
                _ = await proxy_config.new_url()
                Actor.log.info("Residential proxy configured")
            except Exception:
                Actor.log.warning("Could not configure residential proxy — continuing without")

        if config.ANTHROPIC_API_KEY:
            Actor.log.info("LLM fallback enabled (Claude Haiku)")
        else:
            Actor.log.info("LLM fallback disabled — set anthropic_api_key to enable")

        try:
            # Cross-run state lives in a user-namespace named KVS
            # "sift-stack-state", seeded externally by scripts/seed_apify_kvs.py.
            # The run's auto-generated token can't access user-scope stores, so
            # use ApifyClient with the user's APIFY_TOKEN (passed via input or
            # auto-injected by Apify in scheduled runs).
            from apify_client import ApifyClientAsync
            _user_token = (
                actor_input.get("apify_token")
                or os.environ.get("APIFY_TOKEN", "")
            )
            if not _user_token:
                Actor.log.error(
                    "apify_token not supplied — cross-run state cannot be read/written. "
                    "Add apify_token to Actor input (Settings → Integrations → Personal API token)."
                )
                await Actor.fail(status_message="Missing apify_token input")
                return
            _user_client = ApifyClientAsync(_user_token)
            # Store name is configurable so test runs can target a throwaway
            # KVS instead of perturbing production cross-run state. Default is
            # unchanged, so normal runs behave exactly as before.
            _state_kvs_name = (
                actor_input.get("state_kvs_name")
                or os.environ.get("SIFT_STATE_KVS_NAME")
                or "sift-stack-state"
            )
            _kvs_info = await _user_client.key_value_stores().get_or_create(
                name=_state_kvs_name,
            )
            _state_kvs = _user_client.key_value_store(_kvs_info["id"])
            Actor.log.info(
                "Cross-run KVS '%s' opened (id=%s)", _state_kvs_name, _kvs_info["id"],
            )

            # Thin async wrappers mirroring the Actor-SDK KVS API so downstream
            # code reads the same way (`await kvs.get_value(...)` etc.).
            class _UserKVS:
                def __init__(self, client):
                    self._c = client

                async def get_value(self, key, default=None):
                    rec = await self._c.get_record(key)
                    if not rec:
                        return default
                    return rec.get("value")

                async def set_value(self, key, value, content_type=None):
                    if content_type is None:
                        return await self._c.set_record(key, value)
                    return await self._c.set_record(key, value, content_type=content_type)

            kvs = _UserKVS(_state_kvs)

            # ── Load last_run_date ──
            if mode == "daily" and not since_date_override:
                stored = await kvs.get_value("last_run_date")
                if stored:
                    since_date_override = stored
                    Actor.log.info("Daily mode: using stored last_run_date = %s", stored)
                else:
                    Actor.log.info(
                        "Daily mode: no stored last_run_date — scrapers fall back to "
                        "default window (365 days). Seed KVS via scripts/seed_apify_kvs.py "
                        "to pick up where local CLI left off."
                    )

            # ── Load cross-run seen_notice_ids (dedup) ──
            stored_seen = await kvs.get_value("seen_notice_ids") or []
            if isinstance(stored_seen, dict):
                # Legacy shape tolerance — old runs stored a dict
                stored_seen = list(stored_seen.keys()) or list(stored_seen.values())
            seen_ids: set[str] = set(stored_seen)
            Actor.log.info("Loaded %d previously-seen notice IDs from KVS", len(seen_ids))

            # ── Load Travis tax-delinquent state ──
            stored_texdel = await kvs.get_value("travis_texdel_state") or _texdel_state._empty_state()
            _texdel_state.inject_apify_state(stored_texdel)
            Actor.log.info(
                "Loaded Travis texdel state: last_run_date=%s, last_run_apns=%d",
                stored_texdel.get("last_run_date", "(none)"),
                len(stored_texdel.get("last_run_apns") or []),
            )

            # ── Load Bell + Williamson tax-delinquent state ──
            # MUST run before scrape_targets() below: the Bell/Williamson
            # scrapers call load_state(county) during the scrape, which reads
            # the in-memory cache we seed here. Without this inject, prev_apns
            # is always empty → every run reports "first run — seeding".
            for _cty, _key in _GENERIC_TEXDEL:
                _stored = await kvs.get_value(_key) or _gtexdel_state._empty_state()
                _gtexdel_state.inject_apify_state(_cty, _stored)
                Actor.log.info(
                    "Loaded %s texdel state: last_run_date=%s, last_run_apns=%d",
                    _cty,
                    _stored.get("last_run_date", "(none)"),
                    len(_stored.get("last_run_apns") or []),
                )

            # ── Scrape ────────────────────────────────────────────
            from scrapers import scrape_targets
            notices = await scrape_targets(
                targets=[(c, t) for c, t in targets],
                mode=mode,
                since_date=since_date_override or None,
                max_notices=max_notices,
                scraper_kwargs=scraper_kwargs,
            )
            Actor.log.info("Scraped %d total notices before dedup/enrichment", len(notices))
            if max_notices and len(notices) >= max_notices:
                Actor.log.info("Hit max_notices=%d smoke-test cap", max_notices)

            # ── Probate property lookup (async) ──
            probate_notices = [
                n for n in notices
                if n.notice_type == "probate" and n.decedent_name and not n.address
            ]
            if probate_notices:
                try:
                    from property_lookup import lookup_decedent_properties
                    Actor.log.info("Looking up property addresses for %d probate notices...", len(probate_notices))
                    await lookup_decedent_properties(probate_notices)
                except ImportError:
                    Actor.log.warning("property_lookup module not found — skipping")
                except Exception as e:
                    Actor.log.warning("Property lookup failed: %s — continuing", e)

            # ── Snapshot dedup key BEFORE enrichment ──
            # Smarty (Step 6) rewrites notice.address in place, so computing the
            # key at scrape time vs at pipeline-end time produces different keys
            # for the same property. Snapshot once here; reuse for both the
            # filter step AND the seen_ids save step.
            for n in notices:
                n._dedup_key = _notice_dedup_key(n)

            # ── rebuild_dedup_only branch: seed seen_ids + exit ──
            if rebuild_dedup_only:
                added = 0
                for n in notices:
                    if n._dedup_key not in seen_ids:
                        seen_ids.add(n._dedup_key)
                        added += 1
                Actor.log.info(
                    "Rebuild dedup: scraped %d records, added %d raw-address keys "
                    "(%d already present). Total seen_ids: %d",
                    len(notices), added, len(notices) - added, len(seen_ids),
                )
                await kvs.set_value("seen_notice_ids", sorted(seen_ids))
                await kvs.set_value(
                    "travis_texdel_state",
                    _texdel_state.snapshot_apify_state(),
                )
                Actor.log.info("Rebuild complete — exiting without running pipeline")
                return

            # ── Incremental dedup filter ──
            if seen_ids and notices:
                before = len(notices)
                notices = [n for n in notices if n._dedup_key not in seen_ids]
                filtered = before - len(notices)
                if filtered:
                    Actor.log.info(
                        "Incremental dedup: filtered %d already-seen notices (%d remain)",
                        filtered, len(notices),
                    )

            # ── Enrichment pipeline (full CLI parity) ──
            from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

            opts = PipelineOptions(
                skip_parcel_lookup=True,
                skip_vacant_filter=not exclude_vacant,
                skip_commercial_filter=not exclude_commercial,
                skip_entity_filter=not exclude_entities,
                skip_smarty=skip_smarty,
                skip_zillow=skip_zillow,
                skip_tax=skip_tax,
                skip_geocode=skip_geocode,
                skip_obituary=not enable_obituary,
                skip_ancestry=not enable_ancestry,
                skip_entity_research=not research_entities,
                skip_heir_verification=skip_heir_verification,
                max_heir_depth=max_heir_depth,
                skip_dm_address=skip_dm_address,
                tracerfy_tier1=False,
                obituary_workers=obituary_workers,
                skip_zip_filter=skip_zip_filter,
                skip_condo_filter=skip_condo_filter,
                skip_mls_filter=keep_listed,
                source_label="Apify Actor",
            )
            notices = run_enrichment_pipeline(notices, opts)

            if not notices:
                Actor.log.warning("No notices found after enrichment")
                # Still persist state so next run's window narrows
                await kvs.set_value("seen_notice_ids", sorted(seen_ids))
                await kvs.set_value(
                    "last_run_date", datetime.now().strftime("%Y-%m-%d"),
                )
                await kvs.set_value(
                    "travis_texdel_state",
                    _texdel_state.snapshot_apify_state(),
                )
                # Parity with Travis above: persist Bell/Williamson texdel
                # state on the empty-run path too, so a roll change that
                # produced no surviving notices still updates the baseline.
                for _cty, _key in _GENERIC_TEXDEL:
                    await kvs.set_value(_key, _gtexdel_state.snapshot_apify_state(_cty))
                if do_notify_slack and config.SLACK_WEBHOOK_URL:
                    try:
                        from slack_notifier import send_slack_notification
                        send_slack_notification([])
                    except Exception:
                        Actor.log.exception("Empty-run Slack notification failed")
                return

            total = len(notices)

            # ── Tracerfy (opt-in) ──
            tracerfy_stats: dict = {}
            tiers_map: dict = {}
            if enable_tracerfy and config.TRACERFY_API_KEY:
                dp_for_tracerfy = [
                    n for n in notices
                    if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
                ]
                if dp_for_tracerfy:
                    Actor.log.info(
                        "Tracerfy: running on %d DP candidates (%d basic records skipped)",
                        len(dp_for_tracerfy), total - len(dp_for_tracerfy),
                    )
                    try:
                        from tracerfy_skip_tracer import batch_skip_trace
                        tracerfy_stats = batch_skip_trace(dp_for_tracerfy)
                        if tracerfy_stats.get("credits_exhausted"):
                            Actor.log.error("TRACERFY OUT OF CREDITS — skip trace disabled this run")
                        Actor.log.info(
                            "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                            tracerfy_stats.get("matched", 0),
                            tracerfy_stats.get("submitted", 0),
                            tracerfy_stats.get("phones_found", 0),
                            tracerfy_stats.get("emails_found", 0),
                            tracerfy_stats.get("cost", 0.0),
                        )
                    except Exception as e:
                        Actor.log.warning("Tracerfy skip trace failed: %s — continuing", e)
                else:
                    Actor.log.info("Tracerfy: no DP candidates — skipped")

                # Trestle tier scoring (only after Tracerfy — needs phones)
                if enable_trestle and config.TRESTLE_API_KEY:
                    dp_cands = [
                        n for n in notices
                        if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
                    ]
                    if dp_cands:
                        try:
                            from phone_validator import score_record_phones
                            tiers_map = score_record_phones(dp_cands, config.TRESTLE_API_KEY)
                            Actor.log.info(
                                "Trestle scored %d unique phones across %d DP records",
                                len(tiers_map), len(dp_cands),
                            )
                        except Exception as e:
                            Actor.log.warning("Per-record Trestle scoring failed: %s", e)

            # ── Deep Prospecting PDFs → Google Drive ──
            # Uploaded to the same Drive folder as the DataSift CSVs so the Slack
            # links are clickable webViewLinks (Apify KVS records are now Restricted
            # by default and their raw API URLs fail with auth errors).
            pdf_urls: list[dict] = []
            dp_candidates = [] if no_reports else [
                n for n in notices
                if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
            ]
            if no_reports:
                Actor.log.info("Deep-prospecting reports disabled (no_reports=True)")
            if dp_candidates and not (
                config.GOOGLE_DRIVE_FOLDER_ID and config.GOOGLE_SERVICE_ACCOUNT_KEY
            ):
                Actor.log.warning(
                    "Drive creds not set — skipping %d deep-prospecting PDF link(s)",
                    len(dp_candidates),
                )
            elif dp_candidates:
                try:
                    from report_generator import generate_record_pdf
                    from drive_uploader import upload_file
                    report_dir = Path("output/reports")
                    for n in dp_candidates:
                        try:
                            pdf_path = generate_record_pdf(
                                n, output_dir=report_dir, phone_tiers=tiers_map,
                            )
                        except Exception:
                            Actor.log.exception("PDF generation failed for %s", n.address)
                            continue
                        link = upload_file(
                            pdf_path,
                            config.GOOGLE_DRIVE_FOLDER_ID,
                            config.GOOGLE_SERVICE_ACCOUNT_KEY,
                        )
                        if link:
                            pdf_urls.append({"address": n.address, "url": link})
                        else:
                            Actor.log.error("PDF Drive upload failed for %s", n.address)
                    Actor.log.info(
                        "Generated %d deep-prospecting PDFs → Drive (%d records without DP data)",
                        len(pdf_urls), total - len(dp_candidates),
                    )
                except Exception as e:
                    Actor.log.warning("PDF generator import failed: %s", e)

            # ── Legacy flat CSV + push records to Dataset ──
            csv_path = write_csv(notices)
            with open(csv_path, "rb") as f:
                await kvs.set_value("output.csv", f.read(), content_type="text/csv")
            Actor.log.info("Legacy CSV saved to KVS as 'output.csv'")

            # Push structured records to Apify Dataset (one row per notice)
            dataset_rows = []
            for n in notices:
                dataset_rows.append({
                    "date_added": n.date_added,
                    "address": n.address,
                    "city": n.city,
                    "state": n.state,
                    "zip": n.zip,
                    "owner_name": n.owner_name,
                    "business_name": n.business_name,
                    "mailing_address": n.mailing_address,
                    "notice_type": n.notice_type,
                    "county": n.county,
                    "decedent_name": n.decedent_name,
                    "owner_street": n.owner_street,
                    "owner_city": n.owner_city,
                    "owner_state": n.owner_state,
                    "owner_zip": n.owner_zip,
                    "auction_date": n.auction_date,
                    "latitude": n.latitude,
                    "longitude": n.longitude,
                    "dpv_match_code": n.dpv_match_code,
                    "vacant": n.vacant,
                    "rdi": n.rdi,
                    "mls_status": n.mls_status,
                    "estimated_value": n.estimated_value,
                    "equity_percent": n.equity_percent,
                    "property_type": n.property_type,
                    "bedrooms": n.bedrooms,
                    "bathrooms": n.bathrooms,
                    "sqft": n.sqft,
                    "year_built": n.year_built,
                    "parcel_id": n.parcel_id,
                    "tax_delinquent_amount": n.tax_delinquent_amount,
                    "tax_delinquent_years": n.tax_delinquent_years,
                    "owner_deceased": n.owner_deceased,
                    "date_of_death": n.date_of_death,
                    "decision_maker_name": n.decision_maker_name,
                    "dm_confidence": n.dm_confidence,
                    "source_url": n.source_url,
                })
            if dataset_rows:
                await Actor.push_data(dataset_rows)
                Actor.log.info("Pushed %d records to Apify Dataset", len(dataset_rows))

            # ── DataSift CSVs (DMs + Heirs) → KVS + Drive ──
            datasift_csv_urls: list[dict] = []
            drive_links: list[dict] = []
            try:
                from datasift_formatter import write_datasift_by_notice_type
                csv_infos = write_datasift_by_notice_type(
                    notices, keep_government=keep_government,
                )

                kvs_id = kvs._id if hasattr(kvs, "_id") else ""
                for info in csv_infos:
                    key = f"datasift_{info['label'].lower().replace(' ', '_')}.csv"
                    with open(info["path"], "rb") as f:
                        await kvs.set_value(key, f.read(), content_type="text/csv")
                    url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                    datasift_csv_urls.append({
                        "label": info["label"],
                        "url": url,
                        "records": info.get("count", "?"),
                    })
                    Actor.log.info("DataSift CSV (%s) → KVS: %s", info["label"], key)

                # Google Drive upload (if creds set)
                if config.GOOGLE_DRIVE_FOLDER_ID and config.GOOGLE_SERVICE_ACCOUNT_KEY:
                    try:
                        from drive_uploader import upload_file
                        for info in csv_infos:
                            link = upload_file(
                                Path(info["path"]),
                                config.GOOGLE_DRIVE_FOLDER_ID,
                                config.GOOGLE_SERVICE_ACCOUNT_KEY,
                            )
                            if link:
                                Actor.log.info("  %s → Drive: %s", info["label"], link)
                                drive_links.append({"label": info["label"], "url": link})
                            else:
                                Actor.log.error("  %s: Drive upload failed", info["label"])
                    except Exception as e:
                        Actor.log.warning("Drive upload failed: %s — continuing", e)
                else:
                    Actor.log.info("Drive creds not set — skipping Drive upload")
            except Exception as e:
                Actor.log.error("DataSift CSV generation failed: %s", e)

            # ── RAWPIPE: direct API upload to apiv2.reisift.io ──
            # Runs only when the user explicitly enables it. Each CSV
            # (one per notice type) becomes its own DataSift list named
            # "SiftStack {YYYY-MM-DD} - {label}". Skip-trace is opt-in
            # via datasift_no_skip_trace=False because it costs $0.15/owner
            # and requires account balance.
            rawpipe_results: list[dict] = []
            if upload_datasift_api and 'csv_infos' in dir() and csv_infos:
                try:
                    from datasift_api_client import DataSiftAPIClient, DataSiftAPIError
                    from datetime import datetime as _dt

                    Actor.log.info("RAWPIPE: connecting to apiv2.reisift.io…")
                    api_client = DataSiftAPIClient.from_env()

                    for info in csv_infos:
                        # List name is the bare notice-type label, e.g.
                        # "Foreclosure", "Tax Delinquent", "Probate". Records
                        # from different runs merge into a single per-type
                        # list — the date lives on each record's Date Added
                        # column, not on the list name.
                        list_name = info["label"]
                        try:
                            r = api_client.upload_csv(
                                Path(info["path"]),
                                list_name=list_name,
                                tags=["Courthouse Data"],
                                upload_type="new_properties",
                                enrich_property=datasift_do_enrich,
                                enrich_owner=False,  # protect PR/DM mapping
                            )
                            rawpipe_results.append({
                                "label": info["label"],
                                "list_name": list_name,
                                "success": r.get("success"),
                                "line_count": r.get("line_count"),
                                "verified_in_lists": r.get("verified_in_lists"),
                            })
                            Actor.log.info(
                                "RAWPIPE: %s → list=%r records=%s verified=%s",
                                info["label"], list_name,
                                r.get("line_count"), r.get("verified_in_lists"),
                            )
                        except DataSiftAPIError as e:
                            Actor.log.error(
                                "RAWPIPE: %s upload failed (%d): %s",
                                info["label"], e.status, e.body[:200],
                            )
                            rawpipe_results.append({
                                "label": info["label"],
                                "list_name": list_name,
                                "success": False,
                                "error": f"HTTP {e.status}: {e.body[:200]}",
                            })

                    # Skip-trace (opt-in). Soft-fails on "Insufficient balance!".
                    if datasift_do_skip_trace and any(r.get("success") for r in rawpipe_results):
                        try:
                            live_lists = api_client.list_lists(limit=200)
                            by_title = {l["title"]: l["uuid"] for l in live_lists}
                            st_tag = f"skip_traced_{_dt.now().strftime('%Y-%m')}"
                            for r in rawpipe_results:
                                if not r.get("success"):
                                    continue
                                uuid = by_title.get(r["list_name"])
                                if not uuid:
                                    Actor.log.warning(
                                        "Skip-trace: list %r not found post-upload",
                                        r["list_name"],
                                    )
                                    continue
                                try:
                                    st_resp = api_client.skip_trace_list(
                                        uuid, tags=[st_tag], estimate=False,
                                    )
                                    Actor.log.info(
                                        "Skip-trace queued for %r: records=%s cost=$%s",
                                        r["list_name"],
                                        st_resp.get("number_of_records"),
                                        st_resp.get("cost"),
                                    )
                                    r["skip_trace"] = {
                                        "success": True,
                                        "records": st_resp.get("number_of_records"),
                                        "cost": st_resp.get("cost"),
                                    }
                                except DataSiftAPIError as e:
                                    is_balance = "Insufficient balance" in (e.body or "")
                                    log_fn = Actor.log.warning if is_balance else Actor.log.error
                                    log_fn(
                                        "Skip-trace for %r: HTTP %d — %s",
                                        r["list_name"], e.status, e.body[:200],
                                    )
                                    r["skip_trace"] = {
                                        "success": False,
                                        "error": f"HTTP {e.status}: {e.body[:200]}",
                                    }
                        except Exception as e:
                            Actor.log.exception("Skip-trace stage crashed: %s", e)

                except DataSiftAPIError as e:
                    Actor.log.error(
                        "RAWPIPE auth failed (%d): %s — manual upload still available via the KVS links",
                        e.status, e.body[:200],
                    )
                except Exception as e:
                    Actor.log.exception("RAWPIPE crashed: %s — manual upload still available", e)
            elif upload_datasift_api:
                Actor.log.warning("upload_datasift_api=true but no DataSift CSVs were generated")

            # ── Drain Travis texdel buffers to KVS (raw CSVs + reports) ──
            try:
                for filename, text in _texdel_state.drain_apify_raw_csvs()[-7:]:
                    # Retention: keep only the most recent 7 raw CSVs
                    await kvs.set_value(
                        f"travis_tax_raw_{filename}", text, content_type="text/csv",
                    )
                for filename, json_text in _texdel_state.drain_apify_reports():
                    await kvs.set_value(
                        f"travis_texdel_{filename}", json_text, content_type="application/json",
                    )
            except Exception as e:
                Actor.log.warning("Failed to drain texdel buffers to KVS: %s", e)

            # ── Drain Bell + Williamson texdel buffers to KVS (raw XLSX + reports) ──
            try:
                for _cty, _ in _GENERIC_TEXDEL:
                    _slug = _cty.lower()
                    for filename, raw_bytes in _gtexdel_state.drain_apify_raw(_cty)[-7:]:
                        # Retention: keep only the most recent 7 raw XLSX files
                        await kvs.set_value(
                            f"{_slug}_tax_raw_{filename}",
                            raw_bytes,
                            content_type="application/octet-stream",
                        )
                    for filename, json_text in _gtexdel_state.drain_apify_reports(_cty):
                        await kvs.set_value(
                            f"{_slug}_texdel_{filename}",
                            json_text,
                            content_type="application/json",
                        )
            except Exception as e:
                Actor.log.warning("Failed to drain Bell/Williamson texdel buffers to KVS: %s", e)

            # ── Slack notification ──
            elapsed_min = (_time() - pipeline_start) / 60
            cost_breakdown: dict[str, float] = {}
            # Real LLM cost from measured token usage this run (not a flat
            # per-record guess). Reset happens at pipeline start; see below.
            import llm_client
            _llm_usage = llm_client.get_usage()
            if _llm_usage["calls"]:
                cost_breakdown["Anthropic (Haiku)"] = round(
                    llm_client.estimate_cost_usd(_llm_usage), 3
                )
                logging.info(
                    "LLM usage this run: %d calls, %d in / %d out tokens (%s)",
                    _llm_usage["calls"], _llm_usage["input_tokens"],
                    _llm_usage["output_tokens"], _llm_usage["by_model"],
                )
            if tracerfy_stats and tracerfy_stats.get("cost", 0) > 0:
                cost_breakdown["Tracerfy"] = round(tracerfy_stats["cost"], 2)
            smarty_count = sum(1 for n in notices if n.dpv_match_code)
            if smarty_count > 250:
                cost_breakdown["Smarty"] = round((smarty_count - 250) * 0.01, 2)
            zillow_count = sum(1 for n in notices if n.estimated_value)
            if zillow_count > 100:
                cost_breakdown["Zillow"] = round((zillow_count - 100) * 0.01, 2)

            if do_notify_slack and config.SLACK_WEBHOOK_URL:
                try:
                    from slack_notifier import (
                        send_slack_notification, _send_webhook,
                        build_rawpipe_block, build_pdf_block,
                    )
                    # TIGHTFEED message ①: slim daily report. Suppressed
                    # internally when notices is empty.
                    send_slack_notification(
                        notices,
                        cost_breakdown=cost_breakdown,
                    )
                    # TIGHTFEED message ②: RAWPIPE upload summary with
                    # clickable list URLs + per-record addresses + Drive
                    # CSV links. Manual-fallback message (old ③) is gone.
                    rawpipe_block = build_rawpipe_block(
                        rawpipe_results,
                        notices=notices,
                        drive_links=drive_links,
                    )
                    if rawpipe_block:
                        _send_webhook(rawpipe_block)
                    # TIGHTFEED message ④: Deep Prospecting with Drive
                    # CSV link header + per-record PDF links.
                    pdf_block = build_pdf_block(pdf_urls, drive_links=drive_links)
                    if pdf_block:
                        _send_webhook(pdf_block)

                    # Travis tax-delinquent NEW/REPEAT/DROPPED diff block
                    try:
                        from scrapers import tax_delinquent_travis as _tx_tex
                        if _tx_tex.LAST_RUN_DIFF is not None:
                            from scrapers.travis_texdel_state import DiffResult as _Diff
                            from scrapers.travis_texdel_report import format_slack_summary as _fmt_tex
                            diff_obj = _Diff(**_tx_tex.LAST_RUN_DIFF)
                            msg = _fmt_tex(
                                diff_obj,
                                _tx_tex.LAST_RUN_STATS or {},
                                _tx_tex.LAST_RUN_REMOVED or {},
                            )
                            _send_webhook(msg)
                    except Exception:
                        Actor.log.exception("Travis tax-delinquent Slack summary failed")

                    # TRIROLL: Bell + Williamson tax-delinquent diff blocks
                    for _county_name, _mod_name in (("Bell", "tax_delinquent_bell"), ("Williamson", "tax_delinquent_wilco")):
                        try:
                            from importlib import import_module
                            _mod = import_module(f"scrapers.{_mod_name}")
                            if _mod.LAST_RUN_DIFF is not None:
                                from scrapers.tax_delinquent_state import (
                                    DiffResult as _GDiff,
                                    format_slack_summary as _fmt_g,
                                )
                                diff_obj = _GDiff(**_mod.LAST_RUN_DIFF)
                                msg = _fmt_g(
                                    _county_name, diff_obj,
                                    _mod.LAST_RUN_STATS or {},
                                    _mod.LAST_RUN_REMOVED or {},
                                )
                                _send_webhook(msg)
                        except Exception:
                            Actor.log.exception("%s tax-delinquent Slack summary failed", _county_name)

                    Actor.log.info("Slack notification sent")
                except Exception as e:
                    Actor.log.warning("Slack notification failed: %s", e)

            # ── Persist state for next run ──
            # Use the pre-pipeline snapshot key so Smarty mutations don't poison dedup.
            for n in notices:
                key = getattr(n, "_dedup_key", None) or _notice_dedup_key(n)
                seen_ids.add(key)
            await kvs.set_value("seen_notice_ids", sorted(seen_ids))
            await kvs.set_value("last_run_date", datetime.now().strftime("%Y-%m-%d"))
            await kvs.set_value(
                "travis_texdel_state",
                _texdel_state.snapshot_apify_state(),
            )
            Actor.log.info(
                "Saved state: last_run_date=%s, seen_notice_ids=%d, travis_texdel_state APNs=%d",
                datetime.now().strftime("%Y-%m-%d"),
                len(seen_ids),
                len(_texdel_state.snapshot_apify_state().get("last_run_apns") or []),
            )

            # ── Persist Bell + Williamson texdel state for next run ──
            for _cty, _key in _GENERIC_TEXDEL:
                await kvs.set_value(_key, _gtexdel_state.snapshot_apify_state(_cty))
            Actor.log.info(
                "Saved Bell/Williamson texdel state: bell=%d, williamson=%d APNs",
                len(_gtexdel_state.snapshot_apify_state("Bell").get("last_run_apns") or []),
                len(_gtexdel_state.snapshot_apify_state("Williamson").get("last_run_apns") or []),
            )

            Actor.log.info("Done — %d notices exported (%.1f min)", total, elapsed_min)

        except Exception as e:
            Actor.log.error("Pipeline failed: %s", e, exc_info=True)
            try:
                from slack_notifier import notify_error
                notify_error("Apify Actor Pipeline", e, context=f"mode={mode}")
            except Exception:
                pass
            await Actor.fail(status_message=f"Pipeline error: {e}")


# ── CLI mode ──────────────────────────────────────────────────────────


def setup_logging(verbose: bool = False) -> None:
    """Configure logging to both console and date-stamped log file."""
    level = logging.DEBUG if verbose else logging.INFO
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"scrape_{timestamp}.log"

    # Force UTF-8 on console output to avoid cp1252 encoding errors on Windows
    console = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )
    handlers: list[logging.Handler] = [
        console,
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.info("Logging to %s", log_file)


def _run_pdf_import(args) -> None:
    """Run the PDF import pipeline: OCR → parse → enrich → CSV."""
    from pdf_importer import process_pdf
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.pdf_path:
        logging.error("--pdf-path is required for pdf-import mode")
        sys.exit(1)
    if not args.pdf_county:
        logging.error("--pdf-county is required for pdf-import mode")
        sys.exit(1)

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        logging.error("PDF file not found: %s", pdf_path)
        sys.exit(1)

    county = args.pdf_county.strip().title()  # "travis" → "Travis"

    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_pdf(
        pdf_path=pdf_path,
        county=county,
        api_key=api_key,
        date_added=args.pdf_date,
        regex_only=args.regex_only,
    )

    if not notices:
        logging.warning("No records extracted from PDF")
        sys.exit(0)

    # Run unified enrichment pipeline
    opts = PipelineOptions(
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_vacant_filter=not getattr(args, "exclude_vacant", False),
        skip_commercial_filter=not getattr(args, "exclude_commercial", False),
        skip_entity_filter=not getattr(args, "exclude_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        obituary_workers=getattr(args, "obituary_workers", 4),
        source_label=f"PDF import ({pdf_path.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_tax_sale_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_photo_import(args) -> None:
    """Run the photo import pipeline: preprocess → OCR → parse → enrich → CSV."""
    from photo_importer import process_photos
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.folder:
        logging.error("--folder is required for photo-import mode")
        sys.exit(1)
    if not args.photo_county:
        logging.error("--photo-county is required for photo-import mode")
        sys.exit(1)
    if not args.photo_type:
        logging.error("--photo-type is required for photo-import mode")
        sys.exit(1)

    folder = Path(args.folder)
    if not folder.exists() or not folder.is_dir():
        logging.error("Folder not found: %s", folder)
        sys.exit(1)

    county = args.photo_county.strip().title()

    notice_type = args.photo_type.strip().lower()
    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_photos(
        folder=folder,
        county=county,
        notice_type=notice_type,
        date_added=args.photo_date,
        api_key=api_key,
        correct_perspective=not getattr(args, "no_perspective_correct", False),
    )

    if not notices:
        logging.warning("No records extracted from photos")
        sys.exit(0)

    # Run unified enrichment pipeline
    # Skip vacant land filter for notice types without property addresses
    # (probate from court terminals never has property address — would filter everything)
    no_address_types = {"probate", "divorce"}
    opts = PipelineOptions(
        skip_vacant_filter=not getattr(args, "exclude_vacant", False) or notice_type in no_address_types,
        skip_commercial_filter=not getattr(args, "exclude_commercial", False),
        skip_entity_filter=not getattr(args, "exclude_entities", False),
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        obituary_workers=getattr(args, "obituary_workers", 4),
        source_label=f"Photo import ({folder.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_{notice_type}_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_csv_import(args) -> None:
    """Run the CSV re-import pipeline: read CSV → enrich → write new CSV.

    Supports multiple CSV paths (comma-separated) for merging datasets.
    Supports --upload-datasift to format and upload to DataSift after enrichment.
    """
    from data_formatter import read_csv
    from enrichment_pipeline import (
        PipelineOptions,
        detect_existing_enrichment,
        run_enrichment_pipeline,
    )

    # Validate required args
    if not args.csv_path:
        logging.error("--csv-path is required for csv-import mode")
        sys.exit(1)

    # Support multiple CSV paths (comma-separated)
    csv_paths = [Path(p.strip()) for p in args.csv_path.split(",")]
    for cp in csv_paths:
        if not cp.exists():
            logging.error("CSV file not found: %s", cp)
            sys.exit(1)

    county = None
    if args.csv_county:
        county = args.csv_county.strip().title()

    # Read all CSVs → NoticeData, merge
    all_notices = []
    for cp in csv_paths:
        batch = read_csv(cp)
        logging.info("Loaded %d records from %s", len(batch), cp.name)
        all_notices.extend(batch)

    if not all_notices:
        logging.warning("No records found in CSV(s)")
        sys.exit(0)

    # Deduplicate by source_url (notice ID) — keeps most recent
    seen_urls = {}
    for n in all_notices:
        url = getattr(n, "source_url", "") or ""
        if url and url in seen_urls:
            # Keep the one with more enrichment data
            existing = seen_urls[url]
            if (getattr(n, "estimated_value", "") or "") and not (getattr(existing, "estimated_value", "") or ""):
                seen_urls[url] = n
        elif url:
            seen_urls[url] = n
        else:
            # No source_url — keep all (dedup by address later)
            seen_urls[id(n)] = n
    notices = list(seen_urls.values())
    if len(notices) < len(all_notices):
        logging.info("Deduped %d → %d records (by source_url)", len(all_notices), len(notices))

    # Override county if provided (for CSVs without county column)
    if county:
        for n in notices:
            if not n.county.strip():
                n.county = county

    logging.info("Total: %d records from %d CSV(s)", len(notices), len(csv_paths))

    # Build pipeline options
    primary_name = csv_paths[0].name
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_vacant_filter=not getattr(args, "exclude_vacant", False),
        skip_commercial_filter=not getattr(args, "exclude_commercial", False),
        skip_entity_filter=not getattr(args, "exclude_entities", False),
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        obituary_workers=getattr(args, "obituary_workers", 4),
        source_label=f"CSV import ({primary_name})",
    )
    detect_existing_enrichment(notices, opts)
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{csv_paths[0].stem}_reimport_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)

    # DataSift upload (same logic as daily/historical mode)
    if getattr(args, "upload_datasift", False):
        from datasift_formatter import write_datasift_by_notice_type
        from datasift_uploader import upload_datasift_split, upload_to_datasift

        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        csv_infos = write_datasift_by_notice_type(notices)
        for info in csv_infos:
            logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

        if len(csv_infos) > 1:
            upload_result = asyncio.run(
                upload_datasift_split(
                    csv_infos,
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )
        else:
            upload_result = asyncio.run(
                upload_to_datasift(
                    csv_infos[0]["path"],
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )

        if upload_result.get("success"):
            logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
        else:
            logging.error("DataSift upload failed: %s", upload_result.get("message"))

    logging.info("Done — %d records exported", len(notices))


def _run_phone_validate(args) -> None:
    """Run phone validation via Trestle API with DataSift export/upload."""
    import json as _json

    csv_path = getattr(args, "csv_path", None)
    list_name = getattr(args, "list_name", None)
    preset_folder = getattr(args, "preset_folder", None)
    all_records = getattr(args, "all_records", False)

    # Must specify at least one targeting mode
    if not csv_path and not list_name and not preset_folder and not all_records:
        logging.error(
            "phone-validate requires one of: --csv-path, --list-name, --preset-folder, or --all-records"
        )
        sys.exit(1)

    # Parse custom tiers if provided
    tiers = None
    custom_tiers_str = getattr(args, "custom_tiers", None)
    if custom_tiers_str:
        try:
            raw = _json.loads(custom_tiers_str)
            tiers = {k: tuple(v) for k, v in raw.items()}
            logging.info("Using custom tiers: %s", tiers)
        except (_json.JSONDecodeError, ValueError) as e:
            logging.error("Invalid --custom-tiers JSON: %s", e)
            sys.exit(1)

    # Estimate-only mode
    if getattr(args, "estimate", False):
        from phone_validator import estimate_cost, print_estimate

        if csv_path:
            est = estimate_cost(csv_path)
            print_estimate(est)
        else:
            logging.error("--estimate requires --csv-path (export from DataSift first, then estimate)")
            sys.exit(1)
        return

    # Full validation workflow
    from datasift_uploader import run_phone_validation_workflow

    result = asyncio.run(run_phone_validation_workflow(
        list_name=list_name,
        preset_folder=preset_folder,
        all_records=all_records,
        csv_path=csv_path,
        upload_tags=not getattr(args, "no_upload", False),
        api_key=config.TRESTLE_API_KEY or None,
        tiers=tiers,
        add_litigator=getattr(args, "add_litigator", False),
        batch_size=getattr(args, "batch_size", 10),
    ))

    if result.get("success"):
        logging.info("Phone validation: %s", result.get("message", "OK"))
        if result.get("validation_result"):
            vr = result["validation_result"]
            logging.info("  Results: %d scored, %d errors", vr.get("results_count", 0), vr.get("errors_count", 0))
            for tag, count in vr.get("tier_counts", {}).items():
                logging.info("    %s: %d", tag, count)
        if result.get("upload_result"):
            logging.info("  Tag upload: %s", result["upload_result"].get("message", ""))
    else:
        logging.error("Phone validation failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_presets(args) -> None:
    """Run the DataSift filter preset management workflow."""
    from datasift_uploader import run_manage_presets_workflow

    discover = getattr(args, "discover", False)
    add_sold = getattr(args, "add_sold_exclusion", False)
    create_seq = getattr(args, "create_sold_sequence", False)

    # Default to discover if no flags specified
    if not (discover or add_sold or create_seq):
        discover = True

    preset_folders = None
    if getattr(args, "preset_folders", None):
        preset_folders = [f.strip() for f in args.preset_folders.split(",")]

    result = asyncio.run(run_manage_presets_workflow(
        discover=discover,
        add_sold_exclusion=add_sold,
        create_sequence=create_seq,
        preset_folders=preset_folders,
    ))

    if result.get("success"):
        logging.info("Manage presets: %s", result.get("message", "OK"))
        if result.get("discovery"):
            disc = result["discovery"]
            for folder, presets in disc.get("preset_folders", {}).items():
                logging.info("  Folder '%s': %s", folder, presets)
            logging.info("  Sequences: %s", disc.get("sequences", []))
        if result.get("presets"):
            p = result["presets"]
            logging.info("  Updated: %s", p.get("updated", []))
            logging.info("  Failed: %s", p.get("failed", []))
        if result.get("sequence"):
            logging.info("  Sequence: %s", result["sequence"].get("message"))
    else:
        logging.error("Manage presets failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_sold(args) -> None:
    """Run the SiftMap sold properties management workflow."""
    from datasift_uploader import run_manage_sold_workflow

    # Parse counties if provided, otherwise use default (Travis, Bell, Williamson)
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip().title() for c in args.counties.split(",")]

    result = asyncio.run(run_manage_sold_workflow(
        counties=counties,
        months_back=getattr(args, "months_back", 1),
        min_sale_price=getattr(args, "min_sale_price", 1000),
        sold_tag_date=getattr(args, "sold_tag_date", None),
    ))

    if result.get("success"):
        logging.info("Manage sold: %s", result.get("message", "OK"))
        logging.info("  Counties: %s", ", ".join(result.get("counties_processed", [])))
        logging.info("  Total records: %d", result.get("total_records", 0))
    else:
        logging.error("Manage sold failed: %s", result.get("message"))
        sys.exit(1)


def cli_main() -> None:
    """Run as standalone CLI."""
    parser = argparse.ArgumentParser(
        description="SiftStack — full-stack REI operations platform"
    )
    parser.add_argument(
        "mode",
        choices=[
            "daily", "historical", "pdf-import", "photo-import", "dropbox-watch",
            "csv-import", "phone-validate", "manage-sold", "manage-presets",
            # New analysis & workflow modes
            "comp", "rehab", "analyze-deal", "market-analysis", "buyer-prospect",
            "deep-prospect", "lead-manage", "setup-sequences", "niche-sequential",
            "playbook",
        ],
        help=(
            "daily/historical = scrape notices; pdf-import/photo-import = import from files; "
            "dropbox-watch = poll Dropbox; csv-import = re-enrich CSV; "
            "phone-validate = Trestle scoring; manage-sold/manage-presets = DataSift ops; "
            "comp = comparable sales ARV; rehab = rehab cost estimate; "
            "analyze-deal = full deal analysis; market-analysis = zip code scoring; "
            "buyer-prospect = cash buyer lists; deep-prospect = 4-level research; "
            "lead-manage = 4 Pillars qualification; setup-sequences = CRM automation; "
            "niche-sequential = marketing cycle; playbook = SOP generator"
        ),
    )
    parser.add_argument(
        "--counties",
        type=str,
        default=None,
        help='Comma-separated counties to scrape (e.g. "Travis,Bell,Williamson" or "all")',
    )
    parser.add_argument(
        "--types",
        type=str,
        default=None,
        help='Comma-separated notice types (e.g. "foreclosure,probate" or "all")',
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Output separate CSV files per notice type",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override date cutoff (YYYY-MM-DD). Overrides daily/historical mode logic.",
    )
    parser.add_argument(
        "--max-notices",
        type=int,
        default=0,
        help="Stop after scraping this many notices (0 = no limit)",
    )
    parser.add_argument(
        "--min-delinquent-years",
        type=int,
        default=2,
        help="Minimum years delinquent to include for tax_delinquent records (default: 2)",
    )
    parser.add_argument(
        "--min-delinquent-amount",
        type=float,
        default=3000.0,
        help="Minimum $ owed to include for tax_delinquent records (default: 3000)",
    )
    parser.add_argument(
        "--skip-texdel-cleaner",
        action="store_true",
        help="Bypass the Travis tax-delinquent skill cleaner (no overflow merge, no "
             "address contamination strip, no blank-zip resolve). Safety valve.",
    )
    parser.add_argument(
        "--texdel-fixture-csv",
        type=str,
        default=None,
        help="Path to a local CSV that replaces the live Travis Tax Office download. "
             "Used by the cross-run diff verification tests.",
    )
    parser.add_argument(
        "--texdel-skip-target-zip",
        action="store_true",
        help="Disable the skill's Travis target-ZIP filter (keep all 78xxx rows).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--skip-zip-filter",
        action="store_true",
        help="Skip investor zip code filtering (include all zip codes)",
    )
    parser.add_argument(
        "--skip-condo-filter",
        action="store_true",
        help="Skip condo/townhouse filtering (include condos)",
    )
    parser.add_argument(
        "--keep-listed",
        action="store_true",
        help="Keep Active/Pending/Sold/For Rent records (default: filter out after Zillow)",
    )
    parser.add_argument(
        "--keep-government-records",
        action="store_true",
        help="Keep government-owned records in DataSift CSV (Travis County Trustee, City Of Lakeway, etc.); default drops them",
    )
    parser.add_argument(
        "--rebuild-dedup",
        action="store_true",
        help="One-shot: scrape all sources and seed seen_ids.json with raw-address dedup keys, then exit without running the pipeline. Use once after the pre-pipeline dedup-snapshot fix so prior-run Smarty-normalized keys get replaced by raw-address keys.",
    )

    # PDF import arguments
    parser.add_argument(
        "--pdf-path",
        type=str,
        default=None,
        help="Path to scanned tax sale PDF (required for pdf-import mode)",
    )
    parser.add_argument(
        "--pdf-county",
        type=str,
        default=None,
        help='County name for PDF import, e.g. "Travis" (required for pdf-import mode)',
    )
    parser.add_argument(
        "--pdf-date",
        type=str,
        default=None,
        help="Date for PDF records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--regex-only",
        action="store_true",
        help="Skip LLM parsing and use regex only (pdf-import mode)",
    )
    # Photo import arguments
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Path to folder of phone photos (required for photo-import mode)",
    )
    parser.add_argument(
        "--photo-county",
        type=str,
        default=None,
        dest="photo_county",
        help='County name for photo import, e.g. "Travis" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-type",
        type=str,
        default=None,
        dest="photo_type",
        help='Notice type for photo import, e.g. "eviction" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-date",
        type=str,
        default=None,
        help="Date for photo records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--no-perspective-correct",
        action="store_true",
        dest="no_perspective_correct",
        help="Skip perspective correction in photo preprocessing (photo-import mode)",
    )
    # Dropbox watcher arguments
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        dest="poll_interval",
        help="Seconds between Dropbox polls (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        dest="max_polls",
        help="Maximum number of poll cycles (default: infinite)",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        dest="no_delete",
        help="Don't delete photos from Dropbox after processing",
    )
    # CSV import arguments
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help="Path to existing CSV file to re-enrich (required for csv-import mode)",
    )
    parser.add_argument(
        "--csv-county",
        type=str,
        default=None,
        help='County name for CSV import, e.g. "Travis" (sets county for records missing it)',
    )

    parser.add_argument(
        "--skip-smarty",
        action="store_true",
        help="Skip Smarty address standardization",
    )
    parser.add_argument(
        "--skip-zillow",
        action="store_true",
        help="[deprecated no-op — Zillow is off by default; use --enable-zillow to opt in]",
    )
    parser.add_argument(
        "--enable-zillow",
        action="store_true",
        help="Run Zillow property enrichment (off by default — DataSift's "
             "'Enrich Property Information' step fills the same fields post-upload "
             "for free, so paying the per-record Zillow API cost is usually wasted)",
    )
    parser.add_argument(
        "--skip-tax",
        action="store_true",
        help="Skip tax delinquency enrichment",
    )
    parser.add_argument(
        "--skip-obituary",
        action="store_true",
        help="[deprecated no-op — obituary is off by default] Skip obituary search",
    )
    parser.add_argument(
        "--enable-obituary",
        action="store_true",
        help="Opt into obituary search + deceased owner detection (off by default)",
    )
    parser.add_argument(
        "--skip-ancestry",
        action="store_true",
        help="[deprecated no-op — ancestry is off by default] Skip Ancestry.com lookup",
    )
    parser.add_argument(
        "--enable-ancestry",
        action="store_true",
        help="Opt into Ancestry.com lookup (off by default; requires --enable-obituary)",
    )
    parser.add_argument(
        "--skip-geocode",
        action="store_true",
        help="Skip reverse geocode retry for failed Smarty lookups",
    )
    parser.add_argument(
        "--skip-dm-address",
        action="store_true",
        help="Skip decision-maker mailing address lookup",
    )
    parser.add_argument(
        "--skip-heir-verification",
        action="store_true",
        help="Skip heir alive/dead verification loop (still runs obituary search)",
    )
    parser.add_argument(
        "--max-heir-depth",
        type=int,
        default=2,
        help="Max recursion depth for heir verification (default: 2)",
    )
    parser.add_argument(
        "--tracerfy-tier1",
        action="store_true",
        help="Use Tracerfy as primary DM address lookup ($0.02/record)",
    )
    parser.add_argument(
        "--skip-tracerfy",
        action="store_true",
        help="[deprecated no-op — Tracerfy is off by default] Skip Tracerfy batch skip trace",
    )
    parser.add_argument(
        "--enable-tracerfy",
        action="store_true",
        help="Opt into Tracerfy batch skip trace (phones + emails) on DP candidates (off by default)",
    )
    parser.add_argument(
        "--enable-trestle",
        action="store_true",
        help="Opt into Trestle per-phone tier scoring (requires --enable-tracerfy, off by default)",
    )
    parser.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama", "openrouter"],
        default=os.getenv("LLM_BACKEND", "anthropic"),
        help="LLM backend: 'anthropic' (Claude Haiku, paid) or 'ollama' (local, free)",
    )
    parser.add_argument(
        "--research-entities",
        action="store_true",
        help="Research entity-owned properties to find the person behind LLCs/Corps (web search + LLM)",
    )
    parser.add_argument(
        "--obituary-workers",
        type=int,
        default=4,
        help="Concurrent workers for obituary search Phase A (default: 4). Higher = faster but more rate limits.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skeleton pipeline: skip Zillow + Obituary + Ancestry + Tracerfy. "
             "Produces a mailable CSV with property/owner data only (no Zestimate, no deceased detection, no phone lookup). "
             "Typical incremental daily run finishes in 2-5 minutes.",
    )
    # Buy box / filter toggles — control which property types pass through
    # Default: keep everything in your target ZIPs. Use --exclude-* to filter out.
    parser.add_argument(
        "--exclude-vacant",
        action="store_true",
        help="Remove vacant land parcels (default: kept).",
    )
    parser.add_argument(
        "--exclude-commercial",
        action="store_true",
        help="Remove commercial properties (default: kept).",
    )
    parser.add_argument(
        "--exclude-entities",
        action="store_true",
        help="Remove entity-owned records (LLC, Corp, etc.). Default: kept.",
    )
    parser.add_argument(
        "--upload-datasift",
        action="store_true",
        help="Upload results to DataSift.ai via Playwright (requires DATASIFT_EMAIL/PASSWORD)",
    )
    parser.add_argument(
        "--upload-datasift-api",
        action="store_true",
        help="RAWPIPE: upload + enrich + skip-trace via direct REST API to apiv2.reisift.io "
             "(no browser, ~4s vs 2-3min). Same credentials as --upload-datasift.",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip DataSift property enrichment after upload",
    )
    parser.add_argument(
        "--no-skip-trace",
        action="store_true",
        help="Skip DataSift skip trace after upload",
    )
    parser.add_argument(
        "--notify-slack",
        action="store_true",
        help="[deprecated no-op — Slack is on by default when SLACK_WEBHOOK_URL is set]",
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="Suppress the Slack/Discord run summary",
    )
    parser.add_argument(
        "--no-drive",
        action="store_true",
        help="Suppress the Google Drive upload of DataSift CSVs",
    )
    parser.add_argument(
        "--no-reports",
        action="store_true",
        help="Skip deep-prospecting PDF report generation entirely (even for "
             "records that carry a decision-maker / heir / deceased flag)",
    )
    parser.add_argument(
        "--audit-records",
        action="store_true",
        help="Audit DataSift for incomplete records (future: daily check via Playwright)",
    )

    # Phone validation arguments
    parser.add_argument(
        "--list-name",
        type=str,
        default=None,
        help="DataSift list name to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--preset-folder",
        type=str,
        default=None,
        help="DataSift preset folder to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--all-records",
        action="store_true",
        help="Export all DataSift records for phone validation (phone-validate mode)",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Show phone validation cost estimate only, no API calls (phone-validate mode)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip uploading phone tags back to DataSift (phone-validate mode)",
    )
    parser.add_argument(
        "--custom-tiers",
        type=str,
        default=None,
        help='JSON custom tier boundaries, e.g. \'{"Hot": [80,100], "Cold": [0,79]}\' (phone-validate mode)',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Concurrent Trestle API requests per batch (phone-validate mode, default: 10)",
    )
    parser.add_argument(
        "--add-litigator",
        action="store_true",
        help="Include litigator risk check in phone validation (phone-validate mode)",
    )

    # Manage sold arguments
    parser.add_argument(
        "--months-back",
        type=int,
        default=1,
        help="Months of sales to pull from SiftMap (manage-sold mode, default: 1)",
    )
    parser.add_argument(
        "--min-sale-price",
        type=int,
        default=1000,
        help="Min sale price to exclude deed transfers (manage-sold mode, default: 1000)",
    )
    parser.add_argument(
        "--sold-tag-date",
        type=str,
        default=None,
        help="Tag date in YYYY-MM format (manage-sold mode, default: current month)",
    )

    # Manage presets arguments
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover and list all preset folders, presets, and sequences (manage-presets mode)",
    )
    parser.add_argument(
        "--add-sold-exclusion",
        action="store_true",
        help="Update existing presets to exclude Sold status/tag (manage-presets mode)",
    )
    parser.add_argument(
        "--create-sold-sequence",
        action="store_true",
        help="Create Sold Property Cleanup sequence (manage-presets mode)",
    )
    parser.add_argument(
        "--preset-folders",
        type=str,
        default=None,
        help='Comma-separated preset folder names to target (manage-presets mode, default: all)',
    )

    # ── New analysis & workflow mode arguments ────────────────────────
    # Comp analysis
    parser.add_argument("--address", type=str, default=None,
                        help="Property address (comp/rehab/analyze-deal modes)")
    parser.add_argument("--city", type=str, default=None,
                        help="Property city (comp/rehab/analyze-deal modes)")
    parser.add_argument("--zip-code", type=str, default=None,
                        help="Property ZIP code (comp/rehab/analyze-deal modes)")
    parser.add_argument("--radius", type=float, default=0.5,
                        help="Comp search radius in miles (comp mode, default: 0.5)")
    parser.add_argument("--months", type=int, default=6,
                        help="Comp lookback months (comp mode, default: 6)")

    # Rehab estimation
    parser.add_argument("--tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Finish tier 1-4 (rehab mode, default: 2)")
    parser.add_argument("--scope", type=str, default="full", choices=["full", "wholetail"],
                        help="Rehab scope (rehab mode, default: full)")
    parser.add_argument("--region", type=str, default="austin",
                        help="Regional pricing (rehab mode, default: austin)")
    parser.add_argument("--sqft", type=int, default=0,
                        help="Property sqft override (rehab mode)")
    parser.add_argument("--bedrooms", type=int, default=0,
                        help="Bedrooms override (rehab mode)")
    parser.add_argument("--bathrooms", type=float, default=0,
                        help="Bathrooms override (rehab mode)")

    # Deal analysis
    parser.add_argument("--purchase-price", type=float, default=0,
                        help="Purchase price (analyze-deal mode, default: auto-calculate MAO)")
    parser.add_argument("--rehab-tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Rehab tier for deal analysis (default: 2)")
    parser.add_argument("--exit-strategy", type=str, default="flip",
                        choices=["flip", "wholesale", "hold"],
                        help="Exit strategy (analyze-deal mode, default: flip)")

    # Market analysis
    parser.add_argument("--zip-codes", type=str, default=None,
                        help="Comma-separated ZIP codes to analyze (market-analysis mode)")
    parser.add_argument("--monthly-budget", type=float, default=5000,
                        help="Monthly marketing budget for allocation (market-analysis mode)")

    # Buyer prospecting
    parser.add_argument("--min-transactions", type=int, default=2,
                        help="Min transactions to qualify as investor (buyer-prospect mode)")

    # Deep prospecting
    parser.add_argument("--depth", type=int, default=3, choices=[1, 2, 3, 4],
                        help="Research depth level 1-4 (deep-prospect mode, default: 3)")

    # Lead management
    parser.add_argument("--lead-action", type=str, default="qualify",
                        choices=["qualify", "report"],
                        help="Lead management action (lead-manage mode)")

    # Sequence setup
    parser.add_argument("--seq-folder", type=str, default="all",
                        choices=["lead-management", "acquisitions", "transactions",
                                 "deep-prospecting", "default", "all"],
                        help="Sequence folder to create (setup-sequences mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without creating (setup-sequences/niche-sequential)")

    # Niche sequential
    parser.add_argument("--channel", type=str, default="sms",
                        choices=["sms", "call", "mail", "dp"],
                        help="Marketing channel (niche-sequential mode)")
    parser.add_argument("--day", type=int, default=1, choices=[1, 2, 3],
                        help="Cycle day 1-3 (niche-sequential mode)")
    parser.add_argument("--ns-action", type=str, default="execute",
                        choices=["execute", "setup-presets", "status"],
                        help="Niche sequential action (niche-sequential mode)")

    # Playbook
    parser.add_argument("--blueprint", type=str, default="wholesale",
                        choices=["wholesale", "flip", "hold", "hybrid"],
                        help="Investment blueprint (playbook mode)")
    parser.add_argument("--market", type=str, default="austin",
                        help="Target market (playbook mode)")
    parser.add_argument("--team-size", type=int, default=1,
                        help="Team size 1/2/5 (playbook mode)")

    args = parser.parse_args()

    # Expand --fast into the four component skip flags
    if getattr(args, "fast", False):
        args.skip_zillow = True
        args.skip_obituary = True
        args.skip_ancestry = True
        args.skip_tracerfy = True

    # Zillow is now opt-in. DataSift's 'Enrich Property Information' step
    # fills beds/baths/zestimate/sqft/sale-history during upload — running
    # Zillow upstream just doubles the cost. Pass --enable-zillow to override.
    args.skip_zillow = not getattr(args, "enable_zillow", False)

    # Apply LLM backend override from CLI flag
    if hasattr(args, "llm_backend") and args.llm_backend:
        import config as cfg
        cfg.LLM_BACKEND = args.llm_backend
        if args.llm_backend == "ollama":
            logging.info("LLM backend: Ollama (%s)", cfg.OLLAMA_MODEL)
        elif args.llm_backend == "openrouter":
            logging.info("LLM backend: OpenRouter (%s)", cfg.OPENROUTER_MODEL)

    setup_logging(args.verbose)

    if getattr(args, "fast", False):
        logging.info("── --fast mode: skipping Zillow, Obituary, Ancestry, Tracerfy ──")

    # ── Preflight health checks ──────────────────────────────────────
    preflight_failures = _preflight_check(args.mode)
    if preflight_failures:
        for f in preflight_failures:
            logging.error("Preflight FAILED: %s", f)
        # Send Slack alert so unattended runs are visible
        try:
            from slack_notifier import notify_preflight_failure
            notify_preflight_failure(preflight_failures)
        except Exception:
            pass  # Don't fail on notification failure
        sys.exit(1)
    logging.info("Preflight checks passed")

    # ── New analysis & workflow modes ─────────────────────────────────

    if args.mode == "comp":
        if not args.address:
            print("ERROR: --address is required for comp mode")
            return
        from comp_analyzer import run_comp_analysis
        result = run_comp_analysis(
            address=args.address, city=args.city or "", zip_code=args.zip_code or "",
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Comp analysis failed: %s", result["error"])
        else:
            print(f"Comp report: {result['report_path']}")
            arv = result["arv"]
            print(f"ARV: ${arv.arv_low:,.0f} (low) / ${arv.arv_mid:,.0f} (mid) / ${arv.arv_high:,.0f} (high)")
            print(f"Confidence: {arv.confidence} — {arv.confidence_reason}")
        return

    if args.mode == "rehab":
        if not args.address:
            print("ERROR: --address is required for rehab mode")
            return
        from rehab_estimator import run_rehab_estimate
        result = run_rehab_estimate(
            address=args.address, sqft=args.sqft, bedrooms=args.bedrooms or 3,
            bathrooms=args.bathrooms or 2.0, tier=args.tier, scope=args.scope,
            region=args.region,
        )
        full = result["full_estimate"]
        wt = result["wholetail_estimate"]
        print(f"Rehab report: {result['report_path']}")
        print(f"Full rehab: ${full.grand_total:,.0f} ({full.total_weeks:.0f} weeks)")
        print(f"Wholetail:  ${wt.grand_total:,.0f} ({wt.total_weeks:.0f} weeks)")
        return

    if args.mode == "analyze-deal":
        if not args.address:
            print("ERROR: --address is required for analyze-deal mode")
            return
        from deal_analyzer import run_deal_analysis
        result = run_deal_analysis(
            address=args.address, city=args.city or "", zip_code=args.zip_code or "",
            purchase_price=args.purchase_price, rehab_tier=args.rehab_tier,
            exit_strategy=args.exit_strategy, region=args.region,
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Deal analysis failed: %s", result["error"])
        else:
            pkg = result["package"]
            print(f"Deal report: {result['report_path']}")
            print(f"Recommendation: {pkg.recommendation}")
            print(f"ARV: ${pkg.arv.arv_mid:,.0f} | Rehab: ${pkg.rehab_full.grand_total:,.0f}")
            print(f"Flip MAO: ${pkg.mao.flip_mao:,.0f} | Profit: ${pkg.flip.net_profit:,.0f} ({pkg.flip.roi_pct:.0f}% ROI)")
        return

    if args.mode == "market-analysis":
        from market_analyzer import run_market_analysis
        counties = args.counties.split(",") if args.counties else None
        zip_codes = args.zip_codes.split(",") if args.zip_codes else None
        result = run_market_analysis(
            counties=counties, zip_codes=zip_codes,
            monthly_budget=args.monthly_budget,
        )
        if "error" in result:
            logger.error("Market analysis failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Market report: {result['report_path']}")
            print(f"Analyzed {report.total_zips} zips, {report.total_notices} total notices")
            if report.top_zips:
                top = report.top_zips[0]
                print(f"Top zip: {top.zip_code} (score {top.score:.1f}, grade {top.grade})")
        return

    if args.mode == "buyer-prospect":
        from buyer_prospector import run_buyer_prospecting
        counties = args.counties.split(",") if args.counties else None
        result = run_buyer_prospecting(
            counties=counties,
            months_back=args.months_back,
            min_transactions=args.min_transactions,
        )
        if "error" in result:
            logger.error("Buyer prospecting failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Buyer report: {result['report_path']}")
            print(f"Found {report.total_investors} investors")
            print(f"CSV: {result.get('csv_path', 'N/A')}")
        return

    if args.mode == "deep-prospect":
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        if not csv_path:
            csvs = sorted(config.OUTPUT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            csv_path = str(csvs[0]) if csvs else ""
        if not csv_path:
            print("ERROR: --csv-path required or place CSVs in output/")
            return
        import asyncio
        from deep_prospector import run_deep_prospecting
        result = asyncio.run(run_deep_prospecting(
            csv_path=csv_path, depth=args.depth,
            max_records=args.max_notices if hasattr(args, "max_notices") else 0,
        ))
        if "error" in result:
            logger.error("Deep prospecting failed: %s", result["error"])
        else:
            stats = result["stats"]
            print(f"Report: {result['report_path']}")
            print(f"Processed {stats['total']} records at depth {args.depth}")
            print(f"Phones: {stats['phones_found']} | Deceased: {stats['deceased_confirmed']} | DMs: {stats['dms_identified']}")
            print(
                f"LLM tokens: {stats['llm_total_tokens']:,} total | "
                f"{stats['avg_tokens_per_record']:,.0f}/record | "
                f"{stats['llm_input_tokens']:,} in / {stats['llm_output_tokens']:,} out | "
                f"${stats['llm_cost_usd']:.4f}"
            )
            # Slack post (on by default when a webhook is configured; --no-slack suppresses)
            if config.SLACK_WEBHOOK_URL and not getattr(args, "no_slack", False):
                try:
                    from slack_notifier import _send_webhook
                    avg = stats["avg_tokens_per_record"]
                    _send_webhook(
                        "*SiftStack — Deep Prospect*\n"
                        f"{stats['total']} records · ${stats['llm_cost_usd']:.2f} total · "
                        f"${stats['llm_cost_usd'] / max(1, stats['total']):.3f}/record · "
                        f"{stats['avg_tokens_per_record']:,.0f} tokens/record · "
                        f"{stats['phones_found']} phones · {stats['deceased_confirmed']} deceased"
                    )
                except Exception:
                    logger.exception("Deep-prospect Slack post failed")
        return

    if args.mode == "lead-manage":
        from lead_manager import run_lead_management
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        result = run_lead_management(
            action=args.lead_action, csv_path=csv_path,
        )
        if "error" in result:
            logger.error("Lead management failed: %s", result["error"])
        else:
            print(f"STABM report: {result['report_path']}")
            print(f"Total: {result['total']} | Hot: {result['hot']} | Warm: {result['warm']} | Cold: {result['cold']}")
        return

    if args.mode == "setup-sequences":
        from sequence_templates import get_templates, list_templates, preview_sequence
        templates = get_templates(args.seq_folder)
        if args.dry_run:
            print(f"DRY RUN — Would create {len(templates)} sequences in DataSift:")
            for t in templates:
                preview = preview_sequence(t)
                print(f"  [{preview['folder']}] {preview['name']}")
                print(f"    Trigger: {preview['trigger']}")
                print(f"    Actions: {len(preview['actions'])}")
        else:
            print(f"Sequence creation requires Playwright — {len(templates)} templates ready")
            print("Templates defined. DataSift Playwright creation coming in next build.")
            print("\nTemplate list:")
            print(list_templates())
        return

    if args.mode == "niche-sequential":
        from niche_sequential import run_niche_sequential
        result = run_niche_sequential(
            list_name=args.list_name or "",
            channel=args.channel, day=args.day,
            csv_path=args.csv_path if hasattr(args, "csv_path") and args.csv_path else "",
            action=args.ns_action,
        )
        if "error" in result:
            logger.error("Niche sequential failed: %s", result["error"])
        elif "output" in result:
            print(f"Exported: {result['output']}")
            print(f"Channel: {result['channel']}, Day {result['day']}, {result['records']} records")
        elif "presets" in result:
            for p in result["presets"]:
                print(f"  {p['name']}: {p['description']}")
        return

    if args.mode == "playbook":
        from playbook_generator import run_playbook_generator
        result = run_playbook_generator(
            blueprint=args.blueprint, market=args.market,
            team_size=args.team_size,
        )
        print(f"Playbook: {result['playbook_path']}")
        print(f"Blueprint: {result['blueprint'].title()} | Market: {result['market'].title()} | Team: {result['team_size']}")
        return

    # Phone validation mode — separate pipeline
    if args.mode == "phone-validate":
        _run_phone_validate(args)
        return

    # Manage presets mode — filter preset + sequence management
    if args.mode == "manage-presets":
        _run_manage_presets(args)
        return

    # Manage sold properties mode — SiftMap workflow
    if args.mode == "manage-sold":
        _run_manage_sold(args)
        return

    # PDF import mode — separate pipeline
    if args.mode == "pdf-import":
        _run_pdf_import(args)
        return

    # Photo import mode — separate pipeline
    if args.mode == "photo-import":
        _run_photo_import(args)
        return

    # Dropbox watcher mode — polls for new photos
    if args.mode == "dropbox-watch":
        from dropbox_watcher import run_watcher
        run_watcher(
            poll_interval=args.poll_interval,
            delete_after=not getattr(args, "no_delete", False),
            max_polls=args.max_polls,
        )
        return

    # CSV re-import mode — separate pipeline
    if args.mode == "csv-import":
        _run_csv_import(args)
        return

    # Filter scrape targets
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip() for c in args.counties.split(",")]

    types = None
    if args.types and args.types.lower() != "all":
        types = [t.strip() for t in args.types.split(",")]

    targets = _filter_targets(counties, types)
    if not targets:
        logging.error("No scrape targets match the given --counties / --types filters")
        sys.exit(1)

    logging.info(
        "Running %d scrape targets: %s",
        len(targets),
        ", ".join(f"{c}/{t}" for c, t in targets),
    )

    try:
        _run_scrape_pipeline(args, targets)
    except Exception as e:
        logging.exception("Pipeline failed with unhandled error")
        try:
            from slack_notifier import notify_error
            notify_error("Pipeline (top-level)", e, context=f"mode={args.mode}")
        except Exception:
            pass
        sys.exit(1)


def _run_scrape_pipeline(args, targets) -> None:
    """Run the daily/historical scrape → enrich → export → upload pipeline."""
    from scrapers import scrape_targets, list_registered

    registered = list_registered()
    if registered:
        logging.info("Registered scrapers: %s", ", ".join(f"{c}/{t}" for c, t in registered))

    # Filter to only targets that have registered scrapers
    runnable = [(c, t) for c, t in targets if (c.lower(), t.lower()) in dict.fromkeys(registered)]
    skipped = [(c, t) for c, t in targets if (c.lower(), t.lower()) not in dict.fromkeys(registered)]
    if skipped:
        logging.warning(
            "No scraper for: %s (skipping)",
            ", ".join(f"{c}/{t}" for c, t in skipped),
        )

    # Build scraper kwargs (e.g., tax delinquent thresholds + skill cleaner knobs)
    scraper_kwargs = {}
    min_years = getattr(args, "min_delinquent_years", 2)
    min_amount = getattr(args, "min_delinquent_amount", 3000.0)
    if min_years or min_amount:
        scraper_kwargs["min_years"] = min_years
        scraper_kwargs["min_amount"] = min_amount
    fixture_csv = getattr(args, "texdel_fixture_csv", None)
    if fixture_csv:
        scraper_kwargs["fixture_csv"] = fixture_csv
    if getattr(args, "skip_texdel_cleaner", False):
        scraper_kwargs["skip_cleaner"] = True
    if getattr(args, "texdel_skip_target_zip", False):
        scraper_kwargs["skip_target_zip"] = True

    # ── CLI incremental state ────────────────────────────────────────
    # For daily mode without an explicit --since, auto-apply the stored
    # last_run_date. Load the persisted seen-IDs set so we can filter
    # already-processed notices before they hit the pipeline.
    cli_last_run, cli_seen_ids = _load_cli_state()
    since_arg = getattr(args, "since", None)
    if args.mode == "daily" and since_arg is None and cli_last_run:
        since_arg = cli_last_run
        logging.info("Daily mode: using stored last_run_date = %s", cli_last_run)

    notices = asyncio.run(scrape_targets(
        targets=runnable,
        mode=args.mode,
        since_date=since_arg,
        max_notices=getattr(args, "max_notices", None),
        scraper_kwargs=scraper_kwargs,
    )) if runnable else []

    # Handle async probate lookup before pipeline (requires asyncio.run).
    # This runs BEFORE the incremental dedup filter so probate records get
    # their property address populated — otherwise every scraped probate
    # record (with empty address) would share the same composite key and
    # incorrectly collapse against the seed set.
    probate_notices = [n for n in notices if n.notice_type == "probate" and n.decedent_name and not n.address]
    if probate_notices:
        try:
            from property_lookup import lookup_decedent_properties
            logging.info("Looking up property addresses for %d probate notices...", len(probate_notices))
            asyncio.run(lookup_decedent_properties(probate_notices))
        except ImportError:
            logging.warning("property_lookup module not found -- skipping property lookup")
        except Exception as e:
            logging.warning("Property lookup failed: %s -- continuing without lookups", e)

    # Snapshot dedup key ON EACH NOTICE BEFORE THE PIPELINE RUNS. Smarty
    # (Step 6) rewrites notice.address in place, so computing the key at
    # scrape time vs at pipeline-end time produces DIFFERENT keys for the
    # same property. This bug caused ~170 same-property records to slip
    # past the incremental dedup daily because yesterday's stored (post-
    # Smarty) key no longer matched today's (pre-Smarty) check-time key.
    # Snapshot now, use the snapshot for both the filter AND the save.
    for n in notices:
        n._dedup_key = _notice_dedup_key(n)

    # --rebuild-dedup mode: seed seen_ids with every scraped record's raw
    # key and exit without running the enrichment pipeline. Used to migrate
    # an existing seen_ids.json from post-Smarty keys to raw-address keys
    # after the snapshot fix ships — one-shot operation.
    if getattr(args, "rebuild_dedup", False):
        added = 0
        for n in notices:
            if n._dedup_key not in cli_seen_ids:
                cli_seen_ids.add(n._dedup_key)
                added += 1
        logging.info(
            "Rebuild dedup: scraped %d records, added %d raw-address keys "
            "(%d already present). Total seen_ids: %d",
            len(notices), added, len(notices) - added, len(cli_seen_ids),
        )
        _save_cli_state(cli_seen_ids)
        logging.info("Rebuild complete — exiting without running pipeline.")
        return

    # Filter out notices we've already processed in a prior run. Runs AFTER
    # probate CAD lookup so probate records have their property address
    # populated (otherwise every scraped-but-un-located probate record would
    # share the same empty-address composite key). Uses the snapshot key so
    # downstream pipeline mutations to notice.address don't poison the match.
    if cli_seen_ids and notices:
        before = len(notices)
        notices = [n for n in notices if n._dedup_key not in cli_seen_ids]
        filtered = before - len(notices)
        if filtered:
            logging.info(
                "Incremental dedup: filtered %d already-seen notices "
                "(%d remain for processing)", filtered, len(notices),
            )

    # Run unified enrichment pipeline
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    opts = PipelineOptions(
        skip_parcel_lookup=True,  # web scrape notices don't have parcel IDs
        skip_vacant_filter=not getattr(args, "exclude_vacant", False),
        skip_commercial_filter=not getattr(args, "exclude_commercial", False),
        skip_entity_filter=not getattr(args, "exclude_entities", False),
        skip_smarty=getattr(args, "skip_smarty", False),
        skip_zillow=getattr(args, "skip_zillow", False),
        skip_tax=getattr(args, "skip_tax", False),
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=not getattr(args, "enable_obituary", False),
        skip_ancestry=not getattr(args, "enable_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        obituary_workers=getattr(args, "obituary_workers", 4),
        skip_zip_filter=getattr(args, "skip_zip_filter", False),
        skip_condo_filter=getattr(args, "skip_condo_filter", False),
        skip_mls_filter=getattr(args, "keep_listed", False),
        source_label=f"CLI {args.mode}",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No notices found")
        # Still advance last_run.json so tomorrow's scrape window narrows —
        # otherwise a stretch of empty days keeps re-scraping the same window.
        # seen_ids is unchanged (no new records to add).
        if args.mode == "daily":
            _save_cli_state(cli_seen_ids)
        # Send Slack ping even on empty runs so operators know the job
        # ran successfully (vs silently dying). Previously sys.exit(0)
        # fired before the Slack block at the bottom of this function.
        if not getattr(args, "no_slack", False) and config.SLACK_WEBHOOK_URL:
            try:
                from slack_notifier import send_slack_notification
                send_slack_notification([])
            except Exception:
                logging.exception("Slack notification for empty run failed")
        sys.exit(0)

    # Tracerfy batch skip trace — only on DP candidates (deceased owners,
    # heir maps, decision makers). Basic living-owner records get skip traced
    # for free inside DataSift's unlimited plan.
    tiers_map: dict = {}
    tracerfy_stats: dict = {}
    if getattr(args, "enable_tracerfy", False):
        import config as cfg
        if cfg.TRACERFY_API_KEY:
            from tracerfy_skip_tracer import batch_skip_trace
            dp_for_tracerfy = [
                n for n in notices
                if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
            ]
            if dp_for_tracerfy:
                logging.info(
                    "Tracerfy: running on %d DP candidates (%d basic records skipped)",
                    len(dp_for_tracerfy), len(notices) - len(dp_for_tracerfy),
                )
                tracerfy_stats = batch_skip_trace(dp_for_tracerfy)
                if tracerfy_stats.get("credits_exhausted"):
                    logging.error(
                        "TRACERFY OUT OF CREDITS — skip trace disabled for this run. "
                        "Add credits at https://tracerfy.com/billing to resume phone/email lookups."
                    )
                logging.info(
                    "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                    tracerfy_stats.get("matched", 0), tracerfy_stats.get("submitted", 0),
                    tracerfy_stats.get("phones_found", 0), tracerfy_stats.get("emails_found", 0),
                    tracerfy_stats.get("cost", 0.0),
                )
            else:
                logging.info("Tracerfy: no DP candidates — skipped (0 deceased/DM records)")
            # Score every phone (DM #1 + all heirs) — writes per-heir phone_scores
            # into heir_map_json so DataSift Notes and PDFs can surface tier badges.
            if getattr(args, "enable_trestle", False) and cfg.TRESTLE_API_KEY:
                from phone_validator import score_record_phones
                dp_cands = [
                    n for n in notices
                    if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
                ]
                if dp_cands:
                    try:
                        tiers_map = score_record_phones(dp_cands, cfg.TRESTLE_API_KEY)
                        logging.info("Trestle scored %d unique phones across %d DP records",
                                     len(tiers_map), len(dp_cands))
                    except Exception as e:
                        logging.warning("Per-record Trestle scoring failed: %s", e)

    # Write output
    if args.split:
        paths = write_csv_by_type(notices)
        for p in paths:
            logging.info("Output: %s", p)
    else:
        path = write_csv(notices)
        logging.info("Output: %s", path)

    # Persist incremental state so tomorrow's daily run skips these records.
    # Uses the same composite dedup key as the filter step — monotonic growth.
    if args.mode == "daily":
        for n in notices:
            # Use the pre-pipeline snapshot key set earlier (Smarty may have
            # mutated notice.address since then). Fall back to recomputing
            # if the snapshot somehow wasn't attached (defensive).
            key = getattr(n, "_dedup_key", None) or _notice_dedup_key(n)
            cli_seen_ids.add(key)
        _save_cli_state(cli_seen_ids)

    # Generate deep-prospecting PDFs for deceased/DM/heir records.
    # Matches the Apify branch behavior so CLI runs get the same reports —
    # includes the Case Summary section added for deceased-owner records.
    dp_candidates = [] if getattr(args, "no_reports", False) else [
        n for n in notices
        if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
    ]
    if getattr(args, "no_reports", False):
        logging.info("Deep-prospecting reports disabled (--no-reports)")
    if dp_candidates:
        try:
            from report_generator import generate_record_pdf
            report_dir = Path("output/reports")
            generated = 0
            for n in dp_candidates:
                try:
                    pdf_path = generate_record_pdf(
                        n, output_dir=report_dir, phone_tiers=tiers_map,
                    )
                    logging.info("Report generated: %s", pdf_path)
                    generated += 1
                except Exception:
                    logging.exception("PDF generation failed for %s", n.address)
            logging.info(
                "Generated %d/%d deep-prospecting PDFs in %s",
                generated, len(dp_candidates), report_dir,
            )
        except Exception:
            logging.exception("Report generator import failed")

    # Log this run's measured LLM token usage (CLI is a fresh process, so the
    # meter already started at 0 — no reset needed here).
    try:
        import llm_client
        _u = llm_client.get_usage()
        if _u["calls"]:
            logging.info(
                "LLM usage this run: %d calls, %d in / %d out tokens, $%.4f (%s)",
                _u["calls"], _u["input_tokens"], _u["output_tokens"],
                llm_client.estimate_cost_usd(_u), _u["by_model"],
            )
    except Exception:
        logging.exception("LLM usage summary failed")

    # DataSift-ready CSV generation + Drive upload (always run).
    # Playwright auto-upload to DataSift is opt-in via --upload-datasift.
    upload_result = None
    from datasift_formatter import write_datasift_by_notice_type

    csv_infos = write_datasift_by_notice_type(
        notices,
        keep_government=getattr(args, "keep_government_records", False),
    )
    for info in csv_infos:
        logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

    # Always upload to Google Drive unless suppressed — permanent audit trail
    # + one-click manual upload from Drive UI.
    drive_links: list[dict] = []
    if not getattr(args, "no_drive", False) and config.GOOGLE_DRIVE_FOLDER_ID and config.GOOGLE_SERVICE_ACCOUNT_KEY:
        from drive_uploader import upload_file
        logging.info("Uploading DataSift CSVs to Google Drive...")
        for info in csv_infos:
            link = upload_file(
                Path(info["path"]),
                config.GOOGLE_DRIVE_FOLDER_ID,
                config.GOOGLE_SERVICE_ACCOUNT_KEY,
            )
            if link:
                logging.info("  %s → Drive: %s", info["label"], link)
                drive_links.append({"label": info["label"], "url": link})
            else:
                logging.error("  %s: Drive upload failed", info["label"])
    elif getattr(args, "no_drive", False):
        logging.info("Drive upload suppressed via --no-drive")
    else:
        logging.info(
            "Drive creds not set — skipping Drive upload "
            "(set GOOGLE_DRIVE_FOLDER_ID + GOOGLE_SERVICE_ACCOUNT_KEY to enable)"
        )

    upload_result = {
        "success": True,
        "mode": "manual",
        "message": "DataSift CSVs ready (manual upload)",
        "records_uploaded": len(notices),
        "drive_links": drive_links,
        "local_paths": [str(info["path"]) for info in csv_infos],
    }

    # RAWPIPE — direct API upload (no browser). Preferred over Playwright when
    # --upload-datasift-api is set. Falls through to the Playwright path on
    # failure so the existing manual-fallback logic still kicks in.
    if getattr(args, "upload_datasift_api", False):
        from datetime import datetime as _dt
        from datasift_api_client import DataSiftAPIClient, DataSiftAPIError

        do_enrich = not getattr(args, "no_enrich", False)
        # Skip-trace endpoint is not yet captured for the API path. If the
        # user wants skip-trace, fall back to the Playwright path after upload.
        do_skip_trace = not getattr(args, "no_skip_trace", False)
        api_failed = False

        try:
            client = DataSiftAPIClient.from_env()
            api_results = []
            for info in csv_infos:
                # List name is the bare notice-type label, e.g. "Foreclosure",
                # "Tax Delinquent", "Probate". Records from different runs
                # merge into a single per-type list; the date lives on each
                # record's Date Added column.
                list_name = info["label"]
                logging.info(
                    "RAWPIPE: uploading %s as list %r…", info["label"], list_name,
                )
                r = client.upload_csv(
                    Path(info["path"]),
                    list_name=list_name,
                    tags=["Courthouse Data"],
                    upload_type="new_properties",
                    enrich_property=do_enrich,
                    enrich_owner=False,  # protect our PR/DM contact mapping
                )
                api_results.append({"label": info["label"], **r})
                logging.info(
                    "  list=%r  records=%s  verified=%s  time=%s",
                    list_name, r.get("line_count"),
                    r.get("verified_in_lists"), r.get("storage_key", "")[:12],
                )

            ok = all(r.get("success") for r in api_results)
            upload_result = {
                "success": ok,
                "mode": "rawpipe_api",
                "message": "RAWPIPE upload complete" if ok else "RAWPIPE partial failure",
                "records_uploaded": sum(r.get("line_count") or 0 for r in api_results),
                "drive_links": drive_links,
                "local_paths": [str(info["path"]) for info in csv_infos],
                "api_results": api_results,
            }
            if ok:
                logging.info("RAWPIPE: %d list(s) uploaded successfully", len(api_results))
            else:
                logging.error("RAWPIPE upload: partial failure — see api_results")
                api_failed = True

        except DataSiftAPIError as e:
            logging.error("RAWPIPE upload failed (%s): %s", e.status, e.body[:200])
            api_failed = True
        except Exception as e:
            logging.exception("RAWPIPE crashed: %s", e)
            api_failed = True

        # Skip-trace via the direct API. Endpoint: POST /api/internal/property/skip-trace/
        # Body filters by `all_lists` for each uploaded list. Defaults to `clean`
        # records only (skips already-traced). Will 400 with "Insufficient
        # balance!" if the account is out of skip-trace credits — that error
        # is logged but doesn't crash the run.
        if not api_failed and do_skip_trace:
            try:
                from datasift_api_client import DataSiftAPIError
                from datetime import datetime as _dt

                # Re-fetch lists to map title → uuid (uploaded names → uuids)
                live_lists = client.list_lists(limit=200)
                by_title = {l["title"]: l["uuid"] for l in live_lists}

                st_tag = f"skip_traced_{_dt.now().strftime('%Y-%m')}"
                st_results = []
                for r in api_results:
                    title = r["list_name"]
                    list_uuid = by_title.get(title)
                    if not list_uuid:
                        st_results.append({
                            "list_name": title, "success": False,
                            "message": "list not found in account after upload"
                        })
                        continue
                    try:
                        api_resp = client.skip_trace_list(
                            list_uuid, tags=[st_tag], estimate=False,
                        )
                        st_results.append({
                            "list_name": title, "list_uuid": list_uuid,
                            "success": True, "response": api_resp,
                        })
                        logging.info(
                            "RAWPIPE: skip-trace queued for %r (records=%s, cost=$%s)",
                            title,
                            api_resp.get("number_of_records"),
                            api_resp.get("cost"),
                        )
                    except DataSiftAPIError as e:
                        # Treat "Insufficient balance!" as a soft failure —
                        # log loudly but don't abort the run.
                        is_balance = "Insufficient balance" in (e.body or "")
                        level = logging.WARNING if is_balance else logging.ERROR
                        logging.log(
                            level,
                            "RAWPIPE skip-trace for %r: HTTP %d — %s",
                            title, e.status, e.body[:200],
                        )
                        st_results.append({
                            "list_name": title, "list_uuid": list_uuid,
                            "success": False, "status": e.status,
                            "message": e.body[:200],
                        })

                upload_result["skip_trace_result"] = {
                    "success": all(x.get("success") for x in st_results),
                    "tag": st_tag,
                    "results": st_results,
                }
            except Exception as e:
                logging.exception("RAWPIPE skip-trace crashed: %s", e)
                upload_result["skip_trace_result"] = {
                    "success": False, "message": str(e),
                }

        # If RAWPIPE failed, fall through to the Playwright --upload-datasift
        # path below (if the user passed both flags) or stay in manual mode.
        if api_failed and not getattr(args, "upload_datasift", False):
            logging.info(
                "RAWPIPE failed — staying in manual mode. "
                "Pass --upload-datasift to also try the Playwright path as a fallback."
            )

    # Optional Playwright auto-upload to DataSift on top of the Drive flow
    if getattr(args, "upload_datasift", False):
        from datasift_uploader import upload_to_datasift, upload_datasift_split
        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        if len(csv_infos) > 1:
            auto_result = asyncio.run(
                upload_datasift_split(
                    csv_infos, enrich=do_enrich, skip_trace=do_skip_trace,
                )
            )
        else:
            auto_result = asyncio.run(
                upload_to_datasift(
                    csv_infos[0]["path"], enrich=do_enrich, skip_trace=do_skip_trace,
                )
            )

        if auto_result.get("success"):
            logging.info("DataSift upload: %s", auto_result.get("message", "OK"))
            if auto_result.get("enrich_result"):
                logging.info("  Enrich: %s", auto_result["enrich_result"].get("message", ""))
            if auto_result.get("skip_trace_result"):
                logging.info("  Skip trace: %s", auto_result["skip_trace_result"].get("message", ""))
            # Auto succeeded — promote to non-manual mode but keep Drive links
            upload_result = {
                **auto_result,
                "drive_links": drive_links,
                "local_paths": [str(info["path"]) for info in csv_infos],
            }
        else:
            # Auto failed — stay in manual mode; pop CSVs open on macOS for drag-drop
            logging.error("DataSift upload failed: %s", auto_result.get("message"))
            if sys.platform == "darwin":
                import subprocess
                for info in csv_infos:
                    try:
                        subprocess.run(["open", str(info["path"])], check=False)
                        logging.info("Opened locally for manual upload: %s", info["path"])
                    except Exception as e:
                        logging.warning("Failed to open %s: %s", info["path"], e)

    # Slack/Discord notification (default ON; suppress with --no-slack)
    if not getattr(args, "no_slack", False) and config.SLACK_WEBHOOK_URL:
        from slack_notifier import (
            send_slack_notification, _send_webhook,
            build_rawpipe_block,
        )

        # TIGHTFEED ①: slim daily report (suppressed when notices is empty).
        # Real LLM cost from measured token usage this run.
        cli_cost_breakdown: dict[str, float] = {}
        try:
            import llm_client
            _u = llm_client.get_usage()
            if _u["calls"]:
                cli_cost_breakdown["Anthropic (Haiku)"] = round(
                    llm_client.estimate_cost_usd(_u), 3
                )
        except Exception:
            logging.exception("LLM cost computation failed")
        send_slack_notification(
            notices,
            cost_breakdown=cli_cost_breakdown or None,
        )

        # TIGHTFEED ②: RAWPIPE upload summary (CLI-side, fired when
        # --upload-datasift-api was used and produced api_results).
        cli_rawpipe = locals().get("api_results") or []
        if cli_rawpipe:
            rawpipe_block = build_rawpipe_block(
                cli_rawpipe, notices=notices, drive_links=drive_links,
            )
            if rawpipe_block:
                _send_webhook(rawpipe_block)

        # Append the Travis tax-delinquent cross-run diff if the scraper ran
        # this cycle. This is the "sold / paid off" signal the user wants —
        # surfaced as a second webhook so the main run summary stays readable.
        try:
            from scrapers import tax_delinquent_travis as _tx_tex
            from scrapers.travis_texdel_state import DiffResult as _Diff
            from scrapers.travis_texdel_report import format_slack_summary as _fmt_tex
            if _tx_tex.LAST_RUN_DIFF is not None:
                diff_obj = _Diff(**_tx_tex.LAST_RUN_DIFF)
                msg = _fmt_tex(
                    diff_obj,
                    _tx_tex.LAST_RUN_STATS or {},
                    _tx_tex.LAST_RUN_REMOVED or {},
                )
                _send_webhook(msg)
        except Exception:
            logging.exception("Travis tax-delinquent Slack summary failed")

        # TRIROLL: Bell + Williamson tax-delinquent diff blocks
        for _county_name, _mod_name in (("Bell", "tax_delinquent_bell"), ("Williamson", "tax_delinquent_wilco")):
            try:
                from importlib import import_module
                _mod = import_module(f"scrapers.{_mod_name}")
                if _mod.LAST_RUN_DIFF is not None:
                    from scrapers.tax_delinquent_state import (
                        DiffResult as _GDiff,
                        format_slack_summary as _fmt_g,
                    )
                    diff_obj = _GDiff(**_mod.LAST_RUN_DIFF)
                    msg = _fmt_g(
                        _county_name, diff_obj,
                        _mod.LAST_RUN_STATS or {},
                        _mod.LAST_RUN_REMOVED or {},
                    )
                    _send_webhook(msg)
            except Exception:
                logging.exception("%s tax-delinquent Slack summary failed", _county_name)

    # Audit DataSift for incomplete records (future daily check)
    if getattr(args, "audit_records", False):
        logging.info("--audit-records: Not yet implemented. "
                      "Will check DataSift Incomplete tab via Playwright in a future build.")

    logging.info("Done — %d notices exported", len(notices))


# ── Entry point ───────────────────────────────────────────────────────


if __name__ == "__main__":
    if os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"):
        # Running inside Apify platform or with apify run
        asyncio.run(actor_main())
    else:
        # Standalone CLI
        cli_main()

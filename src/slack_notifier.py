"""Send run summary notifications to Slack or Discord via webhook.

Works with both Slack incoming webhooks and Discord webhooks (using the
/slack compatibility endpoint). Set SLACK_WEBHOOK_URL in .env.

Discord webhook URLs should use the /slack suffix:
  https://discord.com/api/webhooks/{id}/{token}/slack
"""

import json
import logging
import os
from datetime import datetime

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Error & Warning Notifications ────────────────────────────────────


def _send_webhook(text: str, webhook_url: str | None = None) -> bool:
    """Send a plain-text message to the configured Slack/Discord webhook."""
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return False
    try:
        resp = requests.post(
            webhook_url,
            json={"text": text},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


def notify_error(
    step: str,
    error: Exception | str,
    *,
    context: str = "",
    webhook_url: str | None = None,
) -> bool:
    """Send an error alert to Slack/Discord.

    Args:
        step: Pipeline step that failed (e.g., "Smarty Standardization").
        error: The exception or error message.
        context: Optional extra context (run_id, record count, etc.).
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":rotating_light: *SiftStack Pipeline Error*",
        f"*Step:* {step}",
        f"*Error:* {error}",
    ]
    if context:
        lines.append(f"*Context:* {context}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    text = "\n".join(lines)
    sent = _send_webhook(text, webhook_url)
    if sent:
        logger.info("Error notification sent to Slack: %s — %s", step, error)
    else:
        logger.warning("Could not send error notification (no webhook or send failed)")
    return sent


def notify_warning(
    message: str,
    *,
    context: str = "",
    webhook_url: str | None = None,
) -> bool:
    """Send a warning alert to Slack/Discord.

    Args:
        message: Warning description.
        context: Optional extra context.
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":warning: *SiftStack Warning*",
        f"{message}",
    ]
    if context:
        lines.append(f"*Context:* {context}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return _send_webhook("\n".join(lines), webhook_url)


def notify_preflight_failure(
    failures: list[str],
    *,
    webhook_url: str | None = None,
) -> bool:
    """Send a preflight check failure alert.

    Args:
        failures: List of failed check descriptions.
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":no_entry: *SiftStack Preflight Failed*",
        f"*{len(failures)} check(s) failed:*",
    ]
    for f in failures:
        lines.append(f"  - {f}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("Pipeline did not start. Fix the above and re-run.")

    return _send_webhook("\n".join(lines), webhook_url)


def _count_by_field(notices: list[NoticeData], field: str) -> dict[str, int]:
    """Count notices grouped by a field value."""
    counts: dict[str, int] = {}
    for n in notices:
        val = getattr(n, field, "") or "unknown"
        counts[val] = counts.get(val, 0) + 1
    return counts


def _by_county_and_type(notices: list[NoticeData]) -> dict[str, dict[str, int]]:
    """Group notice counts by county, then by notice_type within each county."""
    out: dict[str, dict[str, int]] = {}
    for n in notices:
        county = (n.county or "unknown").title()
        ntype = n.notice_type or "unknown"
        out.setdefault(county, {})
        out[county][ntype] = out[county].get(ntype, 0) + 1
    return out


def _zillow_hit_rate(notices: list[NoticeData]) -> tuple[int, int]:
    """Return (enriched, attempted) counts for Zillow property enrichment.

    Attempted = notices with a non-empty address. Enriched = notices where
    Zillow returned data (estimated_value or mls_status populated).
    """
    attempted = sum(1 for n in notices if (n.address or "").strip())
    enriched = sum(
        1 for n in notices
        if (n.address or "").strip()
        and ((n.estimated_value or "").strip() or (n.mls_status or "").strip())
    )
    return enriched, attempted


def _upcoming_auctions(notices: list[NoticeData], days: int = 7) -> list[dict]:
    """Find notices with auction dates in the next N days."""
    now = datetime.now()
    upcoming = []
    for n in notices:
        if not n.auction_date:
            continue
        try:
            auction_dt = datetime.strptime(n.auction_date, "%Y-%m-%d")
            delta = (auction_dt - now).days
            if 0 <= delta <= days:
                upcoming.append({
                    "address": n.address,
                    "city": n.city,
                    "date": n.auction_date,
                    "days_out": delta,
                    "type": n.notice_type,
                })
        except ValueError:
            continue
    return sorted(upcoming, key=lambda x: x["days_out"])


def build_summary(
    notices: list[NoticeData],
    *,
    upload_result: dict | None = None,
    elapsed_min: float = 0,
    api_cost: float = 0,
    cost_breakdown: dict | None = None,
    csv_link: str | None = None,
    pdf_links: list[tuple[str, str]] | None = None,
) -> str:
    """Build a plain-text run summary for Slack/Discord.

    Args:
        notices: All notices from this run.
        upload_result: DataSift upload result dict (optional).
        elapsed_min: Pipeline elapsed time in minutes.
        api_cost: Estimated Haiku API cost for this run (legacy, use cost_breakdown).
        cost_breakdown: Dict of service -> cost, e.g. {"Anthropic": 0.05, "Tracerfy": 0.26}.
    """
    total = len(notices)
    county_type_counts = _by_county_and_type(notices)

    deceased_all = [n for n in notices if n.owner_deceased == "yes"]
    deceased_count = len(deceased_all)
    high_conf = sum(1 for n in deceased_all if n.dm_confidence == "high")
    med_conf = sum(1 for n in deceased_all if n.dm_confidence == "medium")
    low_conf = sum(1 for n in deceased_all if n.dm_confidence == "low")
    estate = sum(
        1 for n in deceased_all
        if n.decision_maker_relationship
        and "estate" in n.decision_maker_relationship.lower()
    )

    upcoming = _upcoming_auctions(notices)
    zillow_enriched, zillow_attempted = _zillow_hit_rate(notices)

    num_counties = len(county_type_counts)
    lines = [
        f"*SiftStack - Daily Report ({datetime.now().strftime('%Y-%m-%d')})*",
        "",
        f"*New notices scraped:* {total}"
        + (f" (across {num_counties} counties)" if num_counties else ""),
        "",
    ]

    # Per-county nested breakdown, biggest first
    for county, type_counts in sorted(
        county_type_counts.items(), key=lambda kv: sum(kv[1].values()), reverse=True
    ):
        county_total = sum(type_counts.values())
        county_notices = [n for n in notices if (n.county or "").title() == county]
        county_deceased = [n for n in county_notices if n.owner_deceased == "yes"]
        county_upcoming = [
            n for n in county_notices
            if n.auction_date and any(a["address"] == n.address for a in upcoming)
        ]

        lines.append(f"*{county} County* — {county_total} records")
        for ntype, count in sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  • {ntype}: {count}")
        if county_deceased:
            c_high = sum(1 for n in county_deceased if n.dm_confidence == "high")
            c_med = sum(1 for n in county_deceased if n.dm_confidence == "medium")
            c_low = sum(1 for n in county_deceased if n.dm_confidence == "low")
            conf_parts = []
            if c_high:
                conf_parts.append(f"High: {c_high}")
            if c_med:
                conf_parts.append(f"Med: {c_med}")
            if c_low:
                conf_parts.append(f"Low: {c_low}")
            conf_str = f" ({', '.join(conf_parts)})" if conf_parts else ""
            lines.append(f"  Deceased: {len(county_deceased)}{conf_str}")
        if county_upcoming:
            lines.append(f"  Upcoming auctions (7d): {len(county_upcoming)}")
        lines.append("")

    # Rollup deceased line
    if deceased_count > 0:
        pct = round(deceased_count / total * 100) if total else 0
        roll = [f"High: {high_conf}", f"Med: {med_conf}"]
        if low_conf:
            roll.append(f"Low: {low_conf}")
        if estate:
            roll.append(f"Estate: {estate}")
        lines.append(f"*Deceased owners (total):* {deceased_count} ({pct}%) — {', '.join(roll)}")

    # Zillow hit rate (signals whether Smarty is helping or hurting)
    if zillow_attempted > 0:
        pct = round(zillow_enriched / zillow_attempted * 100)
        lines.append(
            f"*Zillow enrichment:* {zillow_enriched}/{zillow_attempted} ({pct}%)"
        )

    # Upload result
    if upload_result:
        lines.append("")
        drive_links = upload_result.get("drive_links") or []
        local_paths = upload_result.get("local_paths") or []
        if upload_result.get("mode") == "manual":
            lines.append(
                f"*DataSift CSVs ready (manual upload):* {upload_result.get('records_uploaded', total)} records"
            )
            if drive_links:
                lines.append("*Drive links:*")
                for dl in drive_links:
                    lines.append(f"  {dl['label']}: <{dl['url']}|Download>")
            if local_paths:
                lines.append("*Local files (opened on desktop):*")
                for p in local_paths:
                    lines.append(f"  {p}")
        elif upload_result.get("success"):
            lines.append(
                f"*Uploaded to DataSift:* {upload_result.get('records_uploaded', total)} records"
            )
            if drive_links:
                lines.append("*CSVs in Drive:*")
                for dl in drive_links:
                    lines.append(f"  {dl['label']}: <{dl['url']}|Download>")
        else:
            lines.append(
                f"*DataSift upload FAILED:* {upload_result.get('message', 'unknown error')}"
            )
            if drive_links:
                lines.append("*Drive links (still available):*")
                for dl in drive_links:
                    lines.append(f"  {dl['label']}: <{dl['url']}|Download>")

    # Upcoming auctions
    if upcoming:
        lines.append("")
        lines.append(f"*Upcoming auctions (next 7 days):* {len(upcoming)}")
        for a in upcoming[:5]:
            lines.append(f"  {a['address']}, {a['city']} - {a['date']} ({a['days_out']}d)")
        if len(upcoming) > 5:
            lines.append(f"  ... and {len(upcoming) - 5} more")

    # Pipeline stats
    lines.append("")
    stats = []
    if elapsed_min > 0:
        stats.append(f"Pipeline: {elapsed_min:.0f} min")
    if api_cost > 0 and not cost_breakdown:
        stats.append(f"Haiku API: ${api_cost:.2f}")
    if stats:
        lines.append(" | ".join(stats))

    # File links (CSV + deep-prospecting PDFs)
    if csv_link or pdf_links:
        lines.append("")
        lines.append("*Files*")
        if csv_link:
            lines.append(f"  CSV: <{csv_link}|Download>")
        if pdf_links:
            lines.append(f"  PDFs ({len(pdf_links)}):")
            for addr, url in pdf_links[:10]:
                lines.append(f"    <{url}|{addr}>")
            if len(pdf_links) > 10:
                lines.append(f"    ... and {len(pdf_links) - 10} more")

    # Cost breakdown
    if cost_breakdown:
        total_cost = sum(cost_breakdown.values())
        lines.append("")
        lines.append(f"*Estimated run cost:* ${total_cost:.2f}")
        for service, cost in cost_breakdown.items():
            if cost > 0:
                lines.append(f"  {service}: ${cost:.2f}")

    return "\n".join(lines)


def send_slack_notification(
    notices: list[NoticeData],
    *,
    webhook_url: str | None = None,
    upload_result: dict | None = None,
    elapsed_min: float = 0,
    api_cost: float = 0,
    cost_breakdown: dict | None = None,
    csv_link: str | None = None,
    pdf_links: list[tuple[str, str]] | None = None,
) -> bool:
    """Send a run summary to Slack/Discord webhook.

    Args:
        notices: All notices from this run.
        webhook_url: Slack/Discord webhook URL (defaults to SLACK_WEBHOOK_URL env).
        upload_result: DataSift upload result dict.
        elapsed_min: Pipeline time in minutes.
        api_cost: Estimated API cost (legacy, use cost_breakdown).
        cost_breakdown: Dict of service -> cost for itemized cost reporting.

    Returns:
        True if notification sent successfully.
    """
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("No SLACK_WEBHOOK_URL set, skipping notification")
        return False

    text = build_summary(
        notices,
        upload_result=upload_result,
        elapsed_min=elapsed_min,
        api_cost=api_cost,
        cost_breakdown=cost_breakdown,
        csv_link=csv_link,
        pdf_links=pdf_links,
    )

    sent = _send_webhook(text, webhook_url)
    if sent:
        logger.info("Slack notification sent successfully")
    else:
        logger.error("Failed to send Slack notification")
    return sent

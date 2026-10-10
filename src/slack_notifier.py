"""Send run summary notifications to Slack or Discord via webhook.

Works with both Slack incoming webhooks and Discord webhooks (using the
/slack compatibility endpoint). Set SLACK_WEBHOOK_URL in .env.

Discord webhook URLs should use the /slack suffix:
  https://discord.com/api/webhooks/{id}/{token}/slack
"""

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
    upload_result: dict | None = None,   # accepted for backward-compat; unused
    elapsed_min: float = 0,               # accepted for backward-compat; unused
    api_cost: float = 0,                  # accepted for backward-compat; unused
    cost_breakdown: dict | None = None,
    csv_link: str | None = None,          # accepted for backward-compat; unused
    pdf_links: list[tuple[str, str]] | None = None,  # accepted for backward-compat; unused
) -> str:
    """Build the TIGHTFEED daily-report block.

    Slim by design: header + scrape totals + per-county counts + a one-line
    cost rundown. Everything else (deceased rollups, Zillow ratios, upload
    status, PDF links, upcoming auctions) is either cut entirely or moved
    to its own dedicated `_send_webhook` call so the operator can scan the
    main report in a glance.

    Returns an empty string if there are no notices — caller can use that
    to suppress the webhook entirely on idle days.
    """
    total = len(notices)
    if total == 0:
        return ""

    county_type_counts = _by_county_and_type(notices)
    num_counties = len(county_type_counts)

    lines = [
        f"*SiftStack — {datetime.now().strftime('%Y-%m-%d')}*",
        f"*New notices:* {total}"
        + (f" (across {num_counties} {'county' if num_counties == 1 else 'counties'})" if num_counties else ""),
        "",
    ]

    # Use the friendly labels ("Foreclosure", "Tax Delinquent", ...) rather
    # than internal slugs ("foreclosure", "tax_delinquent") so the daily
    # report matches the DataSift list names exactly.
    try:
        from datasift_formatter import NOTICE_TYPE_TO_LIST
    except ImportError:
        NOTICE_TYPE_TO_LIST = {}

    # Per-county nested breakdown, biggest first
    for county, type_counts in sorted(
        county_type_counts.items(), key=lambda kv: sum(kv[1].values()), reverse=True
    ):
        county_total = sum(type_counts.values())
        lines.append(f"*{county} County* — {county_total}")
        for ntype, count in sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True):
            label = NOTICE_TYPE_TO_LIST.get(ntype, ntype.replace("_", " ").title())
            lines.append(f"  • {label}: {count}")
        lines.append("")

    # One-line cost rundown at the bottom
    if cost_breakdown:
        total_cost = sum(cost_breakdown.values())
        if total_cost > 0:
            parts = [f"{svc} ${cost:.2f}" for svc, cost in cost_breakdown.items() if cost > 0]
            lines.append(f"*Run cost:* ${total_cost:.2f}  ({' · '.join(parts)})")

    return "\n".join(lines).rstrip()


DATASIFT_LIST_URL_TMPL = "https://app.reisift.io/records/properties?list={uuid}"
DATASIFT_LIST_URL_FALLBACK = "https://app.reisift.io/records/properties"


def build_rawpipe_block(
    rawpipe_results: list[dict],
    *,
    notices: list[NoticeData] | None = None,
    drive_links: list[dict] | None = None,
    max_addresses: int = 10,
    title: str = "RAWPIPE upload to DataSift",
) -> str:
    """Build the *RAWPIPE upload to DataSift* webhook block.

    Each successfully-uploaded list gets a clickable header (list URL),
    a Drive CSV link if one exists, and up to `max_addresses` of the
    actual property addresses that landed in it. Failed lists get a
    one-line error with status code + truncated body.

    Returns "" if no results — caller should skip the webhook.
    """
    if not rawpipe_results:
        return ""

    ok = [r for r in rawpipe_results if r.get("success")]
    fail = [r for r in rawpipe_results if not r.get("success")]
    total_records = sum(r.get("line_count") or 0 for r in ok)

    # Build a label -> drive-link lookup so we can append the Drive CSV.
    # `info["label"]` is the list name (e.g. "Foreclosure"), so the keys
    # already match what's in rawpipe_results.
    drive_by_label: dict[str, str] = {
        d["label"]: d["url"] for d in (drive_links or []) if d.get("label") and d.get("url")
    }

    # Group notices by notice_type → list_name so we can show addresses
    # per uploaded list.
    notices_by_list: dict[str, list[NoticeData]] = {}
    if notices:
        try:
            from datasift_formatter import NOTICE_TYPE_TO_LIST
        except ImportError:
            NOTICE_TYPE_TO_LIST = {}
        for n in notices:
            label = NOTICE_TYPE_TO_LIST.get(n.notice_type or "", "")
            if label:
                notices_by_list.setdefault(label, []).append(n)

    lines = [
        f"*{title}:* {len(ok)}/{len(rawpipe_results)} "
        f"list(s) succeeded · {total_records} record(s) total"
    ]

    for r in ok:
        list_name = r.get("list_name", "?")
        uuid = r.get("list_uuid")
        url = DATASIFT_LIST_URL_TMPL.format(uuid=uuid) if uuid else DATASIFT_LIST_URL_FALLBACK
        records = r.get("line_count", "?")
        lines.append("")
        lines.append(f"*<{url}|{list_name}>* — {records} record(s)")

        # Skip-trace status (if it was triggered)
        st = r.get("skip_trace") or {}
        if st:
            if st.get("success"):
                lines.append(
                    f"  :mag: skip-trace queued ({st.get('records', '?')} records, ${st.get('cost', '?')})"
                )
            else:
                lines.append(f"  ✗ skip-trace failed: {str(st.get('error', ''))[:80]}")

        # Drive CSV link (if uploaded to Drive)
        drive_url = drive_by_label.get(list_name)
        if drive_url:
            lines.append(f"  :bookmark: Full CSV in Drive: <{drive_url}|{list_name}.csv>")

        # Per-record address sample
        list_notices = notices_by_list.get(list_name, [])
        for n in list_notices[:max_addresses]:
            addr_parts = [
                p for p in [n.address, n.city, n.state, (n.zip or "")[:5]]
                if (p or "").strip()
            ]
            addr_str = ", ".join(addr_parts)
            # NoticeData carries the full display name (owner_name), not
            # first/last splits — those only exist on the formatted CSV row.
            owner = (n.owner_name or "").strip()
            line = f"  • {addr_str}"
            if owner:
                line += f" — {owner}"
            lines.append(line)
        if len(list_notices) > max_addresses:
            lines.append(f"  …and {len(list_notices) - max_addresses} more")

    for r in fail:
        list_name = r.get("list_name", "?")
        err = str(r.get("error") or r.get("message") or "unknown error")[:120]
        lines.append("")
        lines.append(f"  ✗ `{list_name}` — {err}")

    return "\n".join(lines)


def build_datasift_upload_block(
    result: dict | None,
    *,
    csv_infos: list[dict],
    notices: list[NoticeData] | None = None,
    drive_links: list[dict] | None = None,
    max_addresses: int = 10,
) -> str:
    """Build the *DataSift upload (wizard)* block from a Playwright
    ``upload_datasift_split`` result.

    ``result`` = ``{success, uploads: [{success, message, label}, ...],
    message, batch_tag?, enrich_result?, skip_trace_result?}``. The split
    uploader STOPS at the first failed CSV, so every csv_info past the last
    ``uploads`` entry was never attempted. Reuses the RAWPIPE renderer (list
    header, Drive link, address sample) and appends the batch tag and the
    enrich / skip-trace outcome.

    Returns "" when no upload was attempted — caller should skip the webhook.
    """
    if not result or not csv_infos:
        return ""
    uploads = result.get("uploads")
    if uploads is None:
        uploads = []
    entries: list[dict] = []
    stopped_after = ""
    for i, info in enumerate(csv_infos):
        ln = info.get("list_name") or info.get("label") or "?"
        if i < len(uploads):
            r = uploads[i]
            ok = bool(r.get("success"))
            entries.append({
                "label": info.get("label", ln), "list_name": ln, "success": ok,
                "line_count": info.get("count"),
                "error": "" if ok else (r.get("message") or "upload failed"),
            })
            if not ok:
                stopped_after = str(info.get("label", ln))
        else:
            reason = result.get("message") or "not attempted"
            if stopped_after:
                reason = f"not attempted — uploader stopped after the {stopped_after} failure"
            entries.append({
                "label": info.get("label", ln), "list_name": ln, "success": False,
                "line_count": info.get("count"), "error": reason,
            })

    text = build_rawpipe_block(
        entries, notices=notices, drive_links=drive_links,
        max_addresses=max_addresses, title="DataSift upload (wizard)",
    )
    if not text:
        return ""
    lines = [text]
    tag = (result.get("batch_tag") or "").strip()
    if tag:
        lines.append("")
        lines.append(f":label: batch tag `{tag}` — filter Records by this tag to see exactly what landed")
    for key, label in (("enrich_result", "Enrich"), ("skip_trace_result", "Skip trace")):
        r = result.get(key)
        if not r:
            continue
        mark = ":white_check_mark:" if r.get("success") else "✗"
        lines.append(f"{mark} {label}: {str(r.get('message') or '')[:160]}")
    return "\n".join(lines)


def build_auction_block(notices: list, horizon_days: int = 60, limit: int = 20) -> str:
    """UPCOMING AUCTIONS block for this run — soonest first.

    Built from the run's own notices (no API calls). Shows each sale-dated
    record within ``horizon_days``, flags re-posted (moved) and newly completed
    postings, and counts anything whose date has already passed. Empty string
    when there is nothing to show so the caller can skip the webhook.
    """
    from datetime import datetime as _dt
    rows = []
    passed = 0
    for n in notices or []:
        ad = getattr(n, "auction_date", "") or ""
        if not ad:
            continue
        try:
            d = _dt.strptime(ad[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (d - _dt.now().date()).days
        if days < 0:
            passed += 1
            continue
        if days > horizon_days:
            continue
        rows.append((days, d, n))
    if not rows and not passed:
        return ""
    rows.sort(key=lambda r: (r[0], (getattr(r[2], "county", "") or ""), (getattr(r[2], "address", "") or "")))
    lines = [f"*UPCOMING AUCTIONS* — {len(rows)} within {horizon_days}d"
             + (f" · {passed} already passed" if passed else "")]
    for days, d, n in rows[:limit]:
        who = (getattr(n, "owner_name", "") or getattr(n, "business_name", "") or "—")
        flag = ""
        if getattr(n, "auction_moved_from", ""):
            flag = f"  ↪ moved from {n.auction_moved_from}"
        elif getattr(n, "watch_reason", "") == "completed":
            flag = "  ✚ completed"
        if getattr(n, "auction_date_source", "") == "folder_month":
            flag += "  (date assumed)"
        lines.append(
            f"• {d.strftime('%a %m/%d')} ({days}d)  {getattr(n, 'address', '') or '(no address)'}, "
            f"{getattr(n, 'city', '') or ''}  [{(getattr(n, 'county', '') or '')}]  {who}{flag}"
        )
    if len(rows) > limit:
        lines.append(f"… +{len(rows) - limit} more")
    return "\n".join(lines)


def build_drive_csv_block(drive_links: list[dict] | None) -> str:
    """Build the standalone *Drive CSVs* webhook block.

    One line per CSV created this run — county + list type up front, record
    count, and the timestamped filename as the clickable Drive link. Unlike
    the RAWPIPE block this does NOT depend on a DataSift API upload having
    run, so the Drive links reach Slack on every run that produced data.

    Returns "" if no links — caller should skip the webhook.
    """
    links = [d for d in (drive_links or []) if d.get("url")]
    if not links:
        return ""
    lines = [f"*:file_folder: Drive CSVs this run ({len(links)}):*"]
    for d in links:
        county = (d.get("county") or "").strip()
        label = (d.get("label") or "CSV").strip()
        head = f"{county} · {label}" if county else label
        records = d.get("records")
        rec_txt = f" — {records} record(s)" if records not in (None, "", "?") else ""
        fname = d.get("filename") or f"{label}.csv"
        lines.append(f"• *{head}*{rec_txt} — <{d['url']}|{fname}>")
    return "\n".join(lines)


def build_pdf_block(
    pdf_links: list[dict],
    *,
    drive_links: list[dict] | None = None,
    max_pdfs: int = 10,
) -> str:
    """Build the Deep Prospecting block.

    Leads with the relevant DataSift CSV in Drive (the full reference
    for these candidates), then lists up to `max_pdfs` per-record PDF
    links by address.

    Returns "" if no PDFs — caller should skip the webhook.
    """
    if not pdf_links:
        return ""

    lines = [f"*Deep Prospecting ({len(pdf_links)} records):*"]

    # Top-line Drive CSV links — one per notice type that has data.
    if drive_links:
        csv_parts = [
            f"<{d['url']}|{d.get('filename') or d['label'] + '.csv'}>"
            for d in drive_links if d.get("url")
        ]
        if csv_parts:
            lines.append(f":bookmark: Full CSV in Drive: {' · '.join(csv_parts)}")

    for p in pdf_links[:max_pdfs]:
        addr = p.get("address") or "?"
        url = p.get("url") or ""
        lines.append(f"  • <{url}|{addr}>")
    if len(pdf_links) > max_pdfs:
        lines.append(f"  …and {len(pdf_links) - max_pdfs} more")

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
    if not text:
        # TIGHTFEED: build_summary returns "" for empty runs — suppress the
        # webhook entirely. Quiet days stay quiet.
        logger.info("No notices this run — suppressing daily report")
        return True

    sent = _send_webhook(text, webhook_url)
    if sent:
        logger.info("Slack notification sent successfully")
    else:
        logger.error("Failed to send Slack notification")
    return sent

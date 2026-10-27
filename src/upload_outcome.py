"""Per-list DataSift upload outcomes → Slack alert + JSON manifest + retry.

Every automated upload path (the Playwright wizard in ``datasift_uploader``
and the RAWPIPE API in ``datasift_api_client``) reports per-list results as
plain dicts, and until now a failed list only ever reached the run log. This
module turns those results into one ``UploadFailure`` per list that did not
fully land, then:

  * logs each one at ERROR (the log is the durable record),
  * writes a JSON manifest — ``output/upload_failures/upload_failures_<ts>.json``
    locally, the ``datasift_upload_failures`` KVS key on Apify (rolling
    history, newest first) — naming the CSV, the list, the stage that broke
    and what a human should do about it,
  * sends ONE Slack alert (``:rotating_light:``, error-level — this is a real
    failure, not a warning) with the same information.

Stages, in pipeline order:

  login          nothing was attempted — credentials / login flow
  upload         the list's CSV did not land (wizard step or API call failed)
  not_attempted  the uploader stopped at an earlier failure before reaching it
  enrich         upload landed, "Enrich Property Information" did not start
  skip_trace     upload landed, skip trace did not start
  crash          the uploader raised before producing per-list results

Retry: ``python src/upload_outcome.py retry <manifest.json> [--csv-dir DIR]
[--no-enrich] [--skip-trace]`` re-runs the Playwright uploader on the
lists whose records never landed (login / upload / not_attempted / crash),
and re-runs only the bulk action on lists that landed but missed enrich or
skip trace. It never re-uploads a list that already landed — DataSift merges
by address, but a second upload still costs a wizard run and muddies the
Activity tab.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Rolling history kept under the Apify KVS key.
KVS_KEY = "datasift_upload_failures"
HISTORY_KEEP = 30

STAGE_LOGIN = "login"
STAGE_UPLOAD = "upload"
STAGE_NOT_ATTEMPTED = "not_attempted"
STAGE_ENRICH = "enrich"
STAGE_SKIP_TRACE = "skip_trace"
STAGE_CRASH = "crash"

# Stages whose records are NOT in the CRM — these need the CSV re-uploaded.
RECORDS_MISSING_STAGES = (STAGE_LOGIN, STAGE_UPLOAD, STAGE_NOT_ATTEMPTED, STAGE_CRASH)

_ACTIONS = {
    STAGE_LOGIN: "nothing landed — fix DATASIFT_EMAIL/PASSWORD or the login flow, "
                 "then retry or upload every CSV through the wizard by hand",
    STAGE_UPLOAD: "records are NOT in DataSift — upload this CSV through the wizard "
                  "by hand, or retry from the manifest",
    STAGE_NOT_ATTEMPTED: "never attempted (uploader stopped at an earlier failure) — "
                         "upload this CSV by hand, or retry from the manifest",
    STAGE_CRASH: "uploader crashed — records are NOT in DataSift; upload this CSV "
                 "by hand, or retry from the manifest",
    STAGE_ENRICH: "records landed but were NOT enriched — run Manage → Enrich "
                  "records on this list, or retry from the manifest",
    STAGE_SKIP_TRACE: "records landed but were NOT skip traced — run Send To → "
                      "Skip Trace on this list, or retry from the manifest",
}


@dataclass
class UploadFailure:
    label: str          # CSV label, e.g. "Foreclosure" / "Travis Probate"
    list_name: str      # DataSift list the CSV targets
    stage: str          # one of the STAGE_* constants
    uploader: str       # "playwright" | "api"
    error: str
    csv_path: str = ""
    records: int | None = None
    drive_url: str = ""

    @property
    def action(self) -> str:
        return _ACTIONS.get(self.stage, "check the run log")

    @property
    def records_missing(self) -> bool:
        return self.stage in RECORDS_MISSING_STAGES

    def to_dict(self) -> dict:
        d = asdict(self)
        d["action"] = self.action
        d["records_missing"] = self.records_missing
        return d


# ── Deriving failures from uploader results ──────────────────────────


def _drive_url_for(info: dict, drive_links: list[dict] | None) -> str:
    fname = Path(str(info.get("path", ""))).name
    for d in drive_links or []:
        if not d.get("url"):
            continue
        if d.get("filename") == fname or d.get("label") == info.get("label"):
            return d["url"]
    return ""


def _base(info: dict, drive_links, uploader: str, stage: str, error: str) -> UploadFailure:
    return UploadFailure(
        label=str(info.get("label", "?")),
        list_name=str(info.get("list_name") or info.get("label") or "?"),
        stage=stage,
        uploader=uploader,
        error=str(error or "")[:300],
        csv_path=str(info.get("path", "")),
        records=info.get("count"),
        drive_url=_drive_url_for(info, drive_links),
    )


def _is_login_message(msg: str) -> bool:
    m = (msg or "").lower()
    return "login" in m or "datasift_email" in m or "password" in m


def _bulk_action_failures(
    result: dict, csv_infos: list[dict], drive_links, uploader: str,
) -> list[UploadFailure]:
    """enrich_result / skip_trace_result → per-list failures (landed lists only)."""
    out: list[UploadFailure] = []
    for key, stage in (("enrich_result", STAGE_ENRICH), ("skip_trace_result", STAGE_SKIP_TRACE)):
        r = result.get(key)
        if not r or r.get("success"):
            continue
        per_list = r.get("per_list")
        if per_list:
            failed_lists = {
                p.get("list_name"): p.get("message") or p.get("error") or "failed"
                for p in per_list if not p.get("success")
            }
        else:
            # Single-CSV uploader: one list, one result.
            failed_lists = {
                info.get("list_name") or info.get("label"): r.get("message") or "failed"
                for info in csv_infos
            }
        seen: set[str] = set()
        for info in csv_infos:
            ln = info.get("list_name") or info.get("label")
            if ln in failed_lists and ln not in seen:
                seen.add(ln)
                out.append(_base(info, drive_links, uploader, stage, failed_lists[ln]))
    return out


def failures_from_playwright(
    result: dict, csv_infos: list[dict], drive_links: list[dict] | None = None,
) -> list[UploadFailure]:
    """Interpret ``upload_datasift_split`` / ``upload_to_datasift`` results.

    ``upload_datasift_split`` returns ``{success, uploads: [...], message}``
    and STOPS at the first failed CSV, so every csv_info past the last entry
    in ``uploads`` was never attempted. ``upload_to_datasift`` (single CSV)
    returns the wizard result itself (no ``uploads`` key).
    """
    uploader = "playwright"
    failures: list[UploadFailure] = []
    if not csv_infos:
        return failures

    uploads = result.get("uploads")
    if uploads is None:
        # Single-CSV path.
        if not result.get("success"):
            msg = result.get("message") or "upload failed"
            stage = STAGE_LOGIN if _is_login_message(msg) else STAGE_UPLOAD
            return [_base(csv_infos[0], drive_links, uploader, stage, msg)]
        failures.extend(_bulk_action_failures(result, csv_infos, drive_links, uploader))
        return failures

    if not uploads and not result.get("success"):
        # Split path died before the first wizard run (login / credentials).
        msg = result.get("message") or "login failed"
        return [_base(info, drive_links, uploader, STAGE_LOGIN, msg) for info in csv_infos]

    stopped_after = ""
    for i, info in enumerate(csv_infos):
        if i < len(uploads):
            r = uploads[i]
            if not r.get("success"):
                failures.append(_base(
                    info, drive_links, uploader, STAGE_UPLOAD,
                    r.get("message") or "upload failed",
                ))
                stopped_after = str(info.get("label", "?"))
        else:
            failures.append(_base(
                info, drive_links, uploader, STAGE_NOT_ATTEMPTED,
                f"uploader stopped after the {stopped_after or 'previous'} failure",
            ))

    failures.extend(_bulk_action_failures(result, csv_infos, drive_links, uploader))
    return failures


def failures_from_api(
    results: list[dict],
    csv_infos: list[dict],
    drive_links: list[dict] | None = None,
    *,
    fatal_error: str = "",
) -> list[UploadFailure]:
    """Interpret RAWPIPE per-list results (``{label, list_name, success, error,
    skip_trace?}``). A csv_info with no result entry was never reached: it is
    a ``crash`` when ``fatal_error`` names why, otherwise ``not_attempted``."""
    uploader = "api"
    by_label = {str(r.get("label")): r for r in results or []}
    failures: list[UploadFailure] = []
    for info in csv_infos:
        r = by_label.get(str(info.get("label")))
        if r is None:
            if fatal_error:
                stage = STAGE_LOGIN if _is_login_message(fatal_error) or "auth" in fatal_error.lower() else STAGE_CRASH
                failures.append(_base(info, drive_links, uploader, stage, fatal_error))
            else:
                failures.append(_base(
                    info, drive_links, uploader, STAGE_NOT_ATTEMPTED,
                    "no upload result recorded for this list",
                ))
            continue
        if not r.get("success"):
            failures.append(_base(
                info, drive_links, uploader, STAGE_UPLOAD,
                r.get("error") or r.get("message") or "upload failed",
            ))
            continue
        st = r.get("skip_trace")
        if st and not st.get("success"):
            failures.append(_base(
                info, drive_links, uploader, STAGE_SKIP_TRACE,
                st.get("error") or st.get("message") or "skip trace failed",
            ))
    return failures


def failures_from_exception(
    exc: BaseException, csv_infos: list[dict], drive_links=None, *, uploader: str,
) -> list[UploadFailure]:
    """The uploader raised before returning per-list results: every list is lost."""
    msg = f"{type(exc).__name__}: {exc}"
    return [_base(info, drive_links, uploader, STAGE_CRASH, msg) for info in csv_infos]


# ── Manifest + Slack ────────────────────────────────────────────────


def manifest_dict(failures: list[UploadFailure], *, run_id: str = "") -> dict:
    return {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "run_id": run_id,
        "uploader": sorted({f.uploader for f in failures}),
        "lists_failed": len(failures),
        "records_missing": sum(1 for f in failures if f.records_missing),
        "failures": [f.to_dict() for f in failures],
    }


def write_manifest(failures: list[UploadFailure], out_dir: Path, *, run_id: str = "") -> Path:
    out_dir = Path(out_dir) / "upload_failures"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"upload_failures_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    path.write_text(json.dumps(manifest_dict(failures, run_id=run_id), indent=1), encoding="utf-8")
    return path


def append_history(existing, manifest: dict, keep: int = HISTORY_KEEP) -> list[dict]:
    """Newest-first rolling list for the KVS key."""
    hist = [m for m in (existing or []) if isinstance(m, dict)]
    return [manifest, *hist][:keep]


def build_slack_block(
    failures: list[UploadFailure], *, total_lists: int | None = None, manifest_ref: str = "",
) -> str:
    """One error-level alert naming every list that needs attention."""
    if not failures:
        return ""
    uploaders = "/".join(sorted({f.uploader for f in failures}))
    date = datetime.now().strftime("%Y-%m-%d")
    n = len(failures)
    of = f" of {total_lists}" if total_lists else ""
    missing = sum(1 for f in failures if f.records_missing)
    lines = [
        f":rotating_light: *DataSift upload FAILED — {date}* · {n}{of} list(s) need "
        f"attention ({uploaders}) · {missing} with records NOT in the CRM"
    ]
    for f in failures:
        recs = f" · {f.records} records" if f.records not in (None, "", "?") else ""
        stage = f.stage.replace("_", " ").upper()
        lines.append(f"• *{f.label}* → list \"{f.list_name}\"{recs} — {stage}: {f.error[:140]}")
        lines.append(f"    ↳ do: {f.action}")
        fname = Path(f.csv_path).name if f.csv_path else ""
        if f.drive_url:
            lines.append(f"    ↳ CSV: <{f.drive_url}|{fname or 'Drive'}>")
        elif fname:
            lines.append(f"    ↳ CSV: {fname}")
    if manifest_ref:
        lines.append(f"Manifest: {manifest_ref}")
    return "\n".join(lines)


def report_upload_failures(
    failures: list[UploadFailure],
    *,
    out_dir: Path,
    slack: bool = True,
    webhook_url: str | None = None,
    total_lists: int | None = None,
    run_id: str = "",
    manifest_ref: str = "",
) -> dict:
    """Log + manifest + Slack for one run's upload failures.

    Returns the manifest dict (empty when there were no failures) so the Actor
    can push it to the KVS as well.
    """
    if not failures:
        return {}
    for f in failures:
        logger.error(
            "DataSift upload failure [%s/%s] %s → list %r: %s | %s | csv=%s",
            f.uploader, f.stage, f.label, f.list_name, f.error, f.action, f.csv_path,
        )
    manifest = manifest_dict(failures, run_id=run_id)
    ref = manifest_ref
    try:
        path = write_manifest(failures, out_dir, run_id=run_id)
        manifest["manifest_path"] = str(path)
        logger.error("Upload-failure manifest written: %s", path)
        ref = ref or str(path)
    except Exception as e:  # never let bookkeeping mask the alert
        logger.warning("Could not write upload-failure manifest: %s", e)
    if slack:
        from slack_notifier import _send_webhook

        text = build_slack_block(failures, total_lists=total_lists, manifest_ref=ref)
        if _send_webhook(text, webhook_url):
            logger.info("Upload-failure alert sent to Slack (%d list(s))", len(failures))
        else:
            logger.error("UPLOAD-FAILURE SLACK SEND FAILED — webhook missing or dead")
    return manifest


# ── Retry ────────────────────────────────────────────────────────────


def load_manifest(path: str | Path) -> list[UploadFailure]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for d in data.get("failures", []):
        out.append(UploadFailure(
            label=d.get("label", "?"), list_name=d.get("list_name", "?"),
            stage=d.get("stage", STAGE_UPLOAD), uploader=d.get("uploader", "playwright"),
            error=d.get("error", ""), csv_path=d.get("csv_path", ""),
            records=d.get("records"), drive_url=d.get("drive_url", ""),
        ))
    return out


def _resolve_csv(f: UploadFailure, csv_dir: Path | None) -> Path | None:
    p = Path(f.csv_path) if f.csv_path else None
    if p and p.exists():
        return p
    if csv_dir and p:
        alt = Path(csv_dir) / p.name
        if alt.exists():
            return alt
    return None


async def _rerun_bulk_actions(list_names: list[str], *, enrich: bool, skip_trace: bool) -> dict:
    """Login once and re-run enrich / skip trace on lists that already landed."""
    import os

    from playwright.async_api import async_playwright

    from datasift_uploader import enrich_records, login, skip_trace_records

    email = os.environ.get("DATASIFT_EMAIL", "")
    password = os.environ.get("DATASIFT_PASSWORD", "")
    out: dict = {"enrich": [], "skip_trace": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context(viewport={"width": 1280, "height": 720})).new_page()
        try:
            if not await login(page, email, password):
                out["error"] = "DataSift login failed"
                return out
            for ln in list_names:
                if enrich:
                    r = await enrich_records(page, ln)
                    out["enrich"].append({"list_name": ln, **r})
                if skip_trace:
                    r = await skip_trace_records(page, ln)
                    out["skip_trace"].append({"list_name": ln, **r})
        finally:
            await browser.close()
    return out


def retry(manifest_path: str, *, csv_dir: str | None = None,
          enrich: bool = True, skip_trace: bool = True, slack: bool = True) -> int:
    """Re-run whatever the manifest says is missing. Returns a process exit code."""
    import asyncio

    import config

    failures = load_manifest(manifest_path)
    if not failures:
        print("Manifest has no failures — nothing to retry.")
        return 0

    reupload = [f for f in failures if f.records_missing]
    bulk_only = [f for f in failures if not f.records_missing]
    csv_infos, unresolved = [], []
    seen_lists: set[str] = set()
    for f in reupload:
        p = _resolve_csv(f, Path(csv_dir) if csv_dir else None)
        if p is None:
            unresolved.append(f)
            continue
        csv_infos.append({"path": p, "label": f.label, "list_name": f.list_name, "count": f.records})
        seen_lists.add(f.list_name)
    for f in unresolved:
        print(f"  ! {f.label}: CSV not found locally ({f.csv_path}). Download it from Drive "
              f"and pass --csv-dir, or upload it by hand.")

    new_failures: list[UploadFailure] = list(unresolved)
    if csv_infos:
        from datasift_uploader import upload_datasift_split

        print(f"Re-uploading {len(csv_infos)} CSV(s) through the wizard…")
        try:
            result = asyncio.run(upload_datasift_split(csv_infos, enrich=enrich, skip_trace=skip_trace))
        except Exception as e:
            result = None
            new_failures.extend(failures_from_exception(e, csv_infos, uploader="playwright"))
        if result is not None:
            new_failures.extend(failures_from_playwright(result, csv_infos))

    bulk_lists = [f.list_name for f in bulk_only if f.list_name not in seen_lists]
    bulk_lists = list(dict.fromkeys(bulk_lists))
    if bulk_lists:
        want_enrich = enrich and any(f.stage == STAGE_ENRICH for f in bulk_only)
        want_skip = skip_trace and any(f.stage == STAGE_SKIP_TRACE for f in bulk_only)
        print(f"Re-running bulk actions on {len(bulk_lists)} landed list(s)…")
        try:
            r = asyncio.run(_rerun_bulk_actions(bulk_lists, enrich=want_enrich, skip_trace=want_skip))
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {e}"}
        if r.get("error"):
            for f in bulk_only:
                new_failures.append(UploadFailure(
                    f.label, f.list_name, f.stage, "playwright", r["error"], f.csv_path, f.records, f.drive_url,
                ))
        else:
            by_stage = {STAGE_ENRICH: r.get("enrich", []), STAGE_SKIP_TRACE: r.get("skip_trace", [])}
            for f in bulk_only:
                hit = [x for x in by_stage.get(f.stage, []) if x.get("list_name") == f.list_name]
                if hit and not hit[0].get("success"):
                    new_failures.append(UploadFailure(
                        f.label, f.list_name, f.stage, "playwright",
                        hit[0].get("message") or "failed", f.csv_path, f.records, f.drive_url,
                    ))

    if new_failures:
        report_upload_failures(
            new_failures, out_dir=config.OUTPUT_DIR,
            slack=slack and bool(config.SLACK_WEBHOOK_URL), total_lists=len(failures),
        )
        print(f"{len(new_failures)} list(s) STILL need attention — new manifest written.")
        return 1
    print(f"All {len(failures)} list(s) recovered.")
    if slack and config.SLACK_WEBHOOK_URL:
        from slack_notifier import _send_webhook

        _send_webhook(
            f":white_check_mark: *DataSift upload retry OK* — {len(failures)} list(s) "
            f"recovered from {Path(manifest_path).name}: "
            + ", ".join(dict.fromkeys(f.list_name for f in failures))
        )
    return 0


def _main(argv=None) -> int:
    import argparse

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    p = argparse.ArgumentParser(description="Retry DataSift uploads from a failure manifest.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("retry", help="re-run the lists a manifest says are missing")
    r.add_argument("manifest")
    r.add_argument("--csv-dir", help="folder holding the CSVs if not at their original paths")
    r.add_argument("--no-enrich", action="store_true")
    r.add_argument("--skip-trace", action="store_true", help="also re-run skip trace (off by default)")
    r.add_argument("--no-slack", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    return retry(
        args.manifest, csv_dir=args.csv_dir,
        enrich=not args.no_enrich, skip_trace=args.skip_trace, slack=not args.no_slack,
    )


if __name__ == "__main__":
    raise SystemExit(_main())
